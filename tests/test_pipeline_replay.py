"""End-to-end pipeline runs over recorded SerpApi (and LLM) responses, fully offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import pipeline
from app.serp import cache_key

from .conftest import Recorder, copy_records, find_recorded, recorded_llm_dir

ASIN = "B0F8BVSK21"
URL = f"https://www.amazon.in/dp/{ASIN}"
GSHOP = pipeline.GSHOP
STEP_ORDER = ["step", "anchor", "match", "market", "voices", "summary", "done"]


def _airdopes_records() -> list[Path]:
    """The exact recorded responses the boAt Airdopes 141 Gen 2 check needs, or skip."""
    needed = [
        ("amazon_product", {"asin": ASIN, **pipeline.AMZ}),
        ("google_shopping", {"q": "boAt Airdopes 141 Gen 2", **GSHOP}),
        ("google_shopping", {"q": "boAt Airdopes 141", **GSHOP}),
    ]
    paths = []
    for engine, params in needed:
        p = find_recorded(engine, params)
        if p is None:
            pytest.skip(f"recorded {engine} response not available (run scripts/record_fixtures.py)")
        paths.append(p)
    # immersive product pages for any card in the recorded shopping results
    for p in paths[1:]:
        resp = json.loads(p.read_text(encoding="utf-8"))["response"]
        for r in resp.get("shopping_results") or []:
            tok = r.get("immersive_product_page_token")
            if tok:
                im = find_recorded("google_immersive_product", {"page_token": tok, "more_stores": "true"})
                if im is not None and im not in paths:
                    paths.append(im)
    if len(paths) < 4:
        pytest.skip("recorded immersive product response not available")
    return paths


@pytest.fixture
def airdopes_fixtures(tmp_path) -> Path:
    return copy_records(tmp_path / "fixtures", _airdopes_records())


@pytest.fixture
def airdopes_llm_fixtures(airdopes_fixtures) -> Path:
    llm_dir = recorded_llm_dir()
    if llm_dir is None:
        pytest.skip("recorded LLM cache not available")
    copy_records(airdopes_fixtures / "llm", sorted(llm_dir.glob("*.json")))
    return airdopes_fixtures


def assert_event_order(rec: Recorder) -> None:
    names = rec.names()
    assert names[0] == "step" and rec.events[0][1]["id"] == "parse"
    assert names[-1] == "done"
    firsts = [names.index(n) for n in STEP_ORDER]
    assert firsts == sorted(firsts), names
    for n in ("anchor", "market", "voices", "summary", "done"):
        assert names.count(n) == 1, n
    assert "error" not in names


def assert_offline(rec: Recorder, result: dict) -> None:
    calls = rec.of("serp_call")
    assert calls, "expected SerpApi calls to be reported"
    assert {c["source"] for c in calls} <= {"cache", "fixture"}
    assert not any(c["credit"] for c in calls)
    assert result["credits_used"] == 0


async def test_airdopes_replay_with_recorded_llm(make_deps, recorder, airdopes_llm_fixtures, _no_network):
    deps = make_deps(airdopes_llm_fixtures, airdopes_llm_fixtures)
    result = await pipeline.run(URL, deps, recorder)

    assert result["ok"] is True
    assert_event_order(recorder)
    assert_offline(recorder, result)
    assert _no_network.calls.call_count == 0
    assert all(c["ok"] for c in recorder.of("serp_call"))
    engines = [c["engine"] for c in recorder.of("serp_call")]
    assert engines[0] == "amazon_product"
    assert "google_shopping" in engines and "google_immersive_product" in engines

    # the recorded LLM resolution was actually used
    llm_files = {p.stem for p in (airdopes_llm_fixtures / "llm").glob("*.json")}
    assert deps.llm.used_keys & llm_files

    anchor = recorder.one("anchor")
    assert anchor["asin"] == ASIN
    assert anchor["title"].startswith("boAt Airdopes 141 Gen 2")
    assert anchor["price"] == 799
    assert anchor["mrp"] == 3990  # parsed from "80% off₹3,990", not 80

    stages = [m["stage"] for m in recorder.of("match")]
    assert stages == ["cards", "stores"]

    mkt = recorder.one("market")
    assert mkt["market"]["status"] == "ok"
    assert mkt["market"]["reference"] == 799
    assert mkt["market"]["store_count"] >= 3
    v = mkt["verdict"]
    assert v["label"] == "fair_price"
    assert v["mrp_theatre"] is True
    assert v["deal_price"] == 799 and v["mrp"] == 3990
    assert v["mrp_multiple"] == 5.0
    assert v["headline"].startswith("Claimed 80% off")
    assert v["best_trusted"]["kind"] in {"official", "major", "quick"}

    offers = mkt["offers"]
    anchors = [o for o in offers if o["is_anchor"]]
    assert len(anchors) == 1
    assert anchors[0]["price"] == 799 and anchors[0]["store"] == "Amazon.in (this listing)"
    assert [o["price"] for o in offers] == sorted(o["price"] for o in offers)
    # the listing's own marketplace never vouches for itself
    assert all(not o["in_reference"] for o in offers if "amazon" in o["store"].lower() and not o["is_anchor"])
    assert len({o["store"] for o in offers}) == len(offers)

    summary = recorder.one("summary")
    assert summary["summary"]
    assert summary["generated_by"] in {"llm", "template"}
    assert recorder.one("done") == result


async def test_airdopes_replay_deterministic_only(make_deps, recorder, airdopes_fixtures, _no_network):
    deps = make_deps(airdopes_fixtures, None)  # no LLM cache at all, no key
    assert deps.llm.available is False
    result = await pipeline.run(URL, deps, recorder)

    assert result["ok"] is True
    assert_event_order(recorder)
    assert_offline(recorder, result)
    assert _no_network.calls.call_count == 0

    mkt = recorder.one("market")
    v = mkt["verdict"]
    assert v["label"] in {"good_deal", "fair_price", "above_market", "not_enough_data"}
    assert v["deal_price"] == 799 and v["mrp"] == 3990
    if mkt["market"]["status"] == "ok":
        assert 500 <= mkt["market"]["reference"] <= 1500
        assert v["label"] == "fair_price"
        assert v["mrp_theatre"] is True
    assert any(o["is_anchor"] for o in mkt["offers"])

    summary = recorder.one("summary")
    assert summary["generated_by"] == "template"
    assert summary["voices"] == []
    assert "₹799" in summary["summary"]


async def test_unrecorded_text_query_emits_notice_and_done(make_deps, recorder, tmp_path, _no_network):
    empty = tmp_path / "empty_fixtures"
    empty.mkdir()
    result = await pipeline.run("Sony WH-1000XM5 headphones", make_deps(empty, None), recorder)

    assert result["ok"] is True
    names = recorder.names()
    assert "notice" in names
    assert names[-1] == "done"
    assert "error" not in names
    assert any("isn't recorded" in n["text"] for n in recorder.of("notice"))
    assert all(c["ok"] is False and c["source"] == "fixture" for c in recorder.of("serp_call"))
    assert recorder.one("market")["verdict"]["label"] == "not_enough_data"
    steps = {s["id"]: s["status"] for s in recorder.of("step")}
    assert steps["anchor"] == "warn"
    assert steps["stores"] == "warn"
    assert _no_network.calls.call_count == 0


async def test_unrecorded_asin_still_completes(make_deps, recorder, tmp_path):
    result = await pipeline.run("B0AAAAAAAA", make_deps(tmp_path / "nothing", None), recorder)
    assert result["ok"] is True
    assert recorder.names()[-1] == "done"
    assert recorder.of("step")[0]["kind"] == "amazon_asin"


async def test_unreadable_link_emits_error_only(make_deps, recorder, tmp_path):
    result = await pipeline.run("https://amzn.in/d/abc123X", make_deps(tmp_path / "nothing", None), recorder)
    assert result == {"ok": False}
    assert recorder.names() == ["error"]


async def test_budget_limits_live_spend(tmp_path, recorder, _no_network):
    """With a key in auto mode every miss goes live; the per-check budget caps it (all mocked)."""
    import httpx

    from app.llm import LLM
    from app.serp import SERPAPI_URL, SerpClient

    from .conftest import FAKE_KEY

    route = _no_network.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json={"organic_results": []}))
    serp = SerpClient(FAKE_KEY, tmp_path / "cache", None, mode="auto")
    deps = pipeline.Deps(serp=serp, llm=LLM(None, "m", tmp_path / "cache", None, replay=True), credits_per_check=1)
    try:
        result = await pipeline.run("Sony WH-1000XM5 headphones", deps, recorder)
    finally:
        await serp.aclose()
    assert route.call_count == 1
    assert result["credits_used"] == 1
    assert any("budget" in n["text"].lower() for n in recorder.of("notice"))
    assert recorder.names()[-1] == "done"
    # the cached response is keyed without the api key
    assert (tmp_path / "cache" / f"{cache_key('amazon', {'k': 'Sony WH-1000XM5 headphones', **pipeline.AMZ})}.json").exists()


def test_recorded_amazon_search_never_anchors_wrong_model():
    """Regression: 'HD9252/90' must not anchor on the HD9252/70 listing whose title says '90% less fat'."""
    q = "Philips HD9252/90 air fryer"
    p = find_recorded("amazon", {"k": q, **pipeline.AMZ})
    if p is None:
        pytest.skip("recorded Amazon search not available")
    d = json.loads(p.read_text(encoding="utf-8"))["response"]
    assert any("HD9252/70" in (o.get("title") or "") for o in d.get("organic_results") or [])
    anchor = pipeline.anchor_from_search(d, q)
    assert anchor is None or "HD9252/90" in anchor.title
