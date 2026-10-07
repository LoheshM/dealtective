from __future__ import annotations

import json

import httpx
import pytest

from app.serp import (
    SERPAPI_URL,
    BudgetExceeded,
    CreditBudget,
    ReplayMiss,
    SerpCall,
    SerpClient,
    SerpError,
    cache_key,
    scrub,
)

from .conftest import FAKE_KEY

PARAMS = {"q": "boAt Airdopes 141 Gen 2", "gl": "in", "hl": "en", "google_domain": "google.co.in"}


def record(engine: str, params: dict, response: dict, ts: float = 0.0) -> str:
    return json.dumps({"ts": ts, "engine": engine, "params": params, "response": response})


@pytest.fixture
async def clients(tmp_path):
    made: list[SerpClient] = []

    def _make(**kw) -> SerpClient:
        kw.setdefault("api_key", None)
        kw.setdefault("cache_dir", tmp_path / "cache")
        c = SerpClient(**kw)
        made.append(c)
        return c

    yield _make
    for c in made:
        await c.aclose()


# --- pure helpers ------------------------------------------------------------------------------


def test_cache_key_ignores_api_key_and_order():
    a = cache_key("google_shopping", {"q": "x", "gl": "in", "api_key": "AAA"})
    b = cache_key("google_shopping", {"gl": "in", "api_key": "BBB", "q": "x"})
    c = cache_key("google_shopping", {"q": "x", "gl": "in"})
    assert a == b == c
    assert len(a) == 24


def test_cache_key_depends_on_engine_and_values():
    base = cache_key("google_shopping", {"q": "x"})
    assert base != cache_key("google", {"q": "x"})
    assert base != cache_key("google_shopping", {"q": "y"})
    assert base != cache_key("google_shopping", {"q": "x", "gl": "in"})


def test_cache_key_unicode_stable():
    assert cache_key("amazon", {"k": "₹ deal"}) == cache_key("amazon", {"k": "₹ deal"})


def test_scrub_removes_api_key_and_keeps_other_params():
    data = {"search_parameters": {"engine": "google", "q": "x", "api_key": FAKE_KEY}, "shopping_results": [1]}
    out = scrub(data)
    assert out["search_parameters"] == {"engine": "google", "q": "x"}
    assert out["shopping_results"] == [1]
    # input is not mutated
    assert data["search_parameters"]["api_key"] == FAKE_KEY


def test_scrub_when_api_key_is_the_only_parameter():
    out = scrub({"search_parameters": {"api_key": FAKE_KEY}})
    assert FAKE_KEY not in json.dumps(out)


def test_scrub_without_search_parameters():
    assert scrub({"organic_results": []}) == {"organic_results": []}


def test_budget_reserve_and_refund():
    b = CreditBudget(2)
    b.reserve()
    b.reserve()
    assert b.spent == 2
    with pytest.raises(BudgetExceeded):
        b.reserve()
    b.refund()
    assert b.spent == 1
    b.reserve()
    b.refund()
    b.refund()
    b.refund()
    assert b.spent == 0  # never negative


def test_serp_call_event_marks_only_successful_live_calls_as_credit():
    live = SerpCall("google", "p", {}, "live", 1, True).to_event()
    failed = SerpCall("google", "p", {}, "live", 1, False, "boom").to_event()
    cached = SerpCall("google", "p", {}, "cache", 1, True).to_event()
    assert (live["credit"], failed["credit"], cached["credit"]) == (True, False, False)
    assert failed["error"] == "boom"


# --- modes -----------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["auto", "live", "replay"])
async def test_mode_falls_back_to_replay_without_key(clients, mode):
    assert clients(api_key=None, mode=mode).mode == "replay"
    assert clients(api_key="", mode=mode).mode == "replay"


async def test_mode_kept_with_key(clients):
    assert clients(api_key=FAKE_KEY, mode="auto").mode == "auto"
    assert clients(api_key=FAKE_KEY, mode="live").mode == "live"


async def test_unknown_mode_rejected(tmp_path):
    with pytest.raises(ValueError):
        SerpClient(None, tmp_path, mode="turbo")


async def test_account_without_key_is_none(clients):
    assert await clients().account() is None


# --- replay ------------------------------------------------------------------------------


async def test_replay_miss_raises_and_reports_call(clients, _no_network):
    c = clients(mode="replay")
    seen: list[SerpCall] = []
    with pytest.raises(ReplayMiss):
        await c.search("google_shopping", PARAMS, purpose="test", on_call=seen.append)
    assert len(seen) == 1
    assert seen[0].ok is False
    assert seen[0].source == "fixture"
    assert _no_network.calls.call_count == 0


async def test_replay_serves_fixture_without_network(clients, tmp_path, _no_network):
    fx = tmp_path / "fixtures"
    fx.mkdir()
    body = {"shopping_results": [{"title": "a"}, {"title": "b"}]}
    (fx / f"{cache_key('google_shopping', PARAMS)}.json").write_text(record("google_shopping", PARAMS, body),
                                                                     encoding="utf-8")
    c = clients(mode="replay", fixtures_dir=fx)
    seen: list[SerpCall] = []
    budget = CreditBudget(1)
    data = await c.search("google_shopping", PARAMS, purpose="test", budget=budget, on_call=seen.append)
    assert data == body
    assert seen[0].source == "fixture"
    assert seen[0].ok and seen[0].result_count == 2
    assert budget.spent == 0
    assert _no_network.calls.call_count == 0


async def test_replay_prefers_cache_over_fixture_and_ignores_ttl(clients, tmp_path):
    fx = tmp_path / "fixtures"
    fx.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    key = cache_key("google_shopping", PARAMS)
    (fx / f"{key}.json").write_text(record("google_shopping", PARAMS, {"from": "fixture"}), encoding="utf-8")
    (cache / f"{key}.json").write_text(record("google_shopping", PARAMS, {"from": "cache"}, ts=0),
                                       encoding="utf-8")  # ancient
    c = clients(mode="replay", fixtures_dir=fx, cache_dir=cache)
    seen: list[SerpCall] = []
    data = await c.search("google_shopping", PARAMS, purpose="t", on_call=seen.append)
    assert data == {"from": "cache"}
    assert seen[0].source == "cache"


async def test_none_params_dropped_before_lookup(clients, tmp_path):
    fx = tmp_path / "fixtures"
    fx.mkdir()
    (fx / f"{cache_key('google_shopping', PARAMS)}.json").write_text(record("google_shopping", PARAMS, {"ok": 1}),
                                                                     encoding="utf-8")
    c = clients(mode="replay", fixtures_dir=fx)
    assert await c.search("google_shopping", {**PARAMS, "start": None}, purpose="t") == {"ok": 1}


async def test_async_on_call_is_awaited(clients):
    c = clients(mode="replay")
    seen: list[SerpCall] = []

    async def on_call(call: SerpCall) -> None:
        seen.append(call)

    with pytest.raises(ReplayMiss):
        await c.search("google", {"q": "x"}, purpose="t", on_call=on_call)
    assert len(seen) == 1


# --- live (mocked) ---------------------------------------------------------------------------


async def test_live_call_writes_scrubbed_cache_and_bills_one_credit(clients, tmp_path, _no_network):
    body = {
        "search_metadata": {"status": "Success"},
        "search_parameters": {"engine": "google_shopping", "q": PARAMS["q"], "api_key": FAKE_KEY},
        "shopping_results": [{"title": "boAt Airdopes 141 Gen 2", "price": "₹799"}],
    }
    route = _no_network.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=body))
    c = clients(api_key=FAKE_KEY, mode="auto")
    budget = CreditBudget(3)
    seen: list[SerpCall] = []
    data = await c.search("google_shopping", PARAMS, purpose="t", budget=budget, on_call=seen.append)

    assert data["shopping_results"] == body["shopping_results"]
    assert route.call_count == 1
    sent = route.calls[0].request.url.params
    assert sent["engine"] == "google_shopping"
    assert sent["q"] == PARAMS["q"]
    assert sent["api_key"] == FAKE_KEY
    assert budget.spent == 1
    assert seen[0].source == "live" and seen[0].ok and seen[0].to_event()["credit"] is True
    assert "api_key" not in seen[0].params

    files = list((tmp_path / "cache").glob("*.json"))
    assert [f.stem for f in files] == [cache_key("google_shopping", PARAMS)]
    text = files[0].read_text(encoding="utf-8")
    assert FAKE_KEY not in text
    assert "api_key" not in text
    rec = json.loads(text)
    assert rec["engine"] == "google_shopping"
    assert rec["params"] == PARAMS
    assert rec["response"]["search_parameters"] == {"engine": "google_shopping", "q": PARAMS["q"]}

    # second identical call is served from the fresh cache, free
    seen.clear()
    again = await c.search("google_shopping", PARAMS, purpose="t", budget=budget, on_call=seen.append)
    assert again["shopping_results"] == body["shopping_results"]
    assert route.call_count == 1
    assert seen[0].source == "cache"
    assert budget.spent == 1


async def test_live_mode_bypasses_cache(clients, tmp_path, _no_network):
    cache = tmp_path / "cache"
    cache.mkdir()
    key = cache_key("google", {"q": "x"})
    (cache / f"{key}.json").write_text(record("google", {"q": "x"}, {"from": "cache"}, ts=9e12), encoding="utf-8")
    route = _no_network.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json={"from": "live"}))
    c = clients(api_key=FAKE_KEY, mode="live", cache_dir=cache)
    assert await c.search("google", {"q": "x"}, purpose="t") == {"from": "live"}
    assert route.call_count == 1


async def test_auto_mode_refetches_expired_cache(clients, tmp_path, _no_network):
    cache = tmp_path / "cache"
    cache.mkdir()
    key = cache_key("google", {"q": "x"})
    (cache / f"{key}.json").write_text(record("google", {"q": "x"}, {"from": "old"}, ts=0), encoding="utf-8")
    route = _no_network.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json={"from": "live"}))
    c = clients(api_key=FAKE_KEY, mode="auto", cache_dir=cache, ttl_hours=1)
    assert await c.search("google", {"q": "x"}, purpose="t") == {"from": "live"}
    assert route.call_count == 1


async def test_failed_live_call_refunds_budget(clients, _no_network):
    route = _no_network.get(SERPAPI_URL).mock(
        return_value=httpx.Response(401, json={"error": "Invalid API key. Your API key should be here: ..."}))
    c = clients(api_key=FAKE_KEY, mode="auto")
    budget = CreditBudget(1)
    seen: list[SerpCall] = []
    with pytest.raises(SerpError, match="Invalid API key"):
        await c.search("google", {"q": "x"}, purpose="t", budget=budget, on_call=seen.append)
    assert route.call_count == 1
    assert budget.spent == 0
    assert seen[0].ok is False and seen[0].source == "live"
    assert seen[0].to_event()["credit"] is False
    assert not list((c.cache_dir).glob("*.json"))  # errors are not cached


async def test_error_field_with_200_is_serp_error(clients, _no_network):
    _no_network.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json={"error": "Something broke"}))
    c = clients(api_key=FAKE_KEY, mode="auto")
    with pytest.raises(SerpError, match="Something broke"):
        await c.search("google", {"q": "x"}, purpose="t")


async def test_http_error_without_body_message(clients, _no_network):
    _no_network.get(SERPAPI_URL).mock(return_value=httpx.Response(503, json={}))
    c = clients(api_key=FAKE_KEY, mode="auto")
    with pytest.raises(SerpError, match="HTTP 503"):
        await c.search("google", {"q": "x"}, purpose="t")


async def test_non_json_response_is_serp_error(clients, _no_network):
    _no_network.get(SERPAPI_URL).mock(return_value=httpx.Response(502, text="<html>Bad gateway</html>"))
    c = clients(api_key=FAKE_KEY, mode="auto")
    budget = CreditBudget(1)
    with pytest.raises(SerpError, match="non-JSON"):
        await c.search("google", {"q": "x"}, purpose="t", budget=budget)
    assert budget.spent == 0


async def test_network_error_retried_once_then_serp_error(clients, _no_network):
    route = _no_network.get(SERPAPI_URL).mock(side_effect=httpx.ConnectError("down"))
    c = clients(api_key=FAKE_KEY, mode="auto")
    budget = CreditBudget(1)
    with pytest.raises(SerpError, match="network error: ConnectError"):
        await c.search("google", {"q": "x"}, purpose="t", budget=budget)
    assert route.call_count == 2
    assert budget.spent == 0


async def test_network_error_then_success(clients, _no_network):
    route = _no_network.get(SERPAPI_URL).mock(
        side_effect=[httpx.ConnectError("blip"), httpx.Response(200, json={"organic_results": []})])
    c = clients(api_key=FAKE_KEY, mode="auto")
    assert await c.search("google", {"q": "x"}, purpose="t") == {"organic_results": []}
    assert route.call_count == 2


async def test_no_results_error_is_valid_empty_result(clients, _no_network):
    body = {
        "search_parameters": {"engine": "google_shopping", "q": "zzzz", "api_key": FAKE_KEY},
        "error": "Google hasn't returned any results for this query.",
    }
    _no_network.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=body))
    c = clients(api_key=FAKE_KEY, mode="auto")
    budget = CreditBudget(1)
    seen: list[SerpCall] = []
    data = await c.search("google_shopping", {"q": "zzzz"}, purpose="t", budget=budget, on_call=seen.append)
    assert "hasn't returned any results" in data["error"]
    assert seen[0].ok is True
    assert seen[0].result_count == 0
    assert budget.spent == 1  # SerpApi bills empty results
    cached = next(c.cache_dir.glob("*.json")).read_text(encoding="utf-8")
    assert FAKE_KEY not in cached


async def test_budget_exhausted_makes_no_request(clients, _no_network):
    route = _no_network.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json={}))
    c = clients(api_key=FAKE_KEY, mode="auto")
    seen: list[SerpCall] = []
    with pytest.raises(BudgetExceeded):
        await c.search("google", {"q": "x"}, purpose="t", budget=CreditBudget(0), on_call=seen.append)
    assert route.call_count == 0
    assert seen[0].ok is False


async def test_cache_hit_does_not_need_budget(clients, tmp_path, _no_network):
    cache = tmp_path / "cache"
    cache.mkdir()
    key = cache_key("google", {"q": "x"})
    (cache / f"{key}.json").write_text(record("google", {"q": "x"}, {"news_results": [1, 2, 3]}, ts=9e12),
                                       encoding="utf-8")
    c = clients(api_key=FAKE_KEY, mode="auto", cache_dir=cache)
    seen: list[SerpCall] = []
    await c.search("google", {"q": "x"}, purpose="t", budget=CreditBudget(0), on_call=seen.append)
    assert seen[0].source == "cache" and seen[0].result_count == 3


async def test_account_parses_free_endpoint(clients, _no_network):
    from app.serp import ACCOUNT_URL

    _no_network.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json={
        "plan_name": "Free", "total_searches_left": 42, "this_hour_searches": 1,
        "account_rate_limit_per_hour": 50, "api_key": FAKE_KEY}))
    acct = await clients(api_key=FAKE_KEY, mode="auto").account()
    assert acct == {"plan": "Free", "searches_left": 42, "this_hour": 1, "hourly_limit": 50}


async def test_account_errors_are_swallowed(clients, _no_network):
    from app.serp import ACCOUNT_URL

    _no_network.get(ACCOUNT_URL).mock(side_effect=httpx.ConnectError("down"))
    assert await clients(api_key=FAKE_KEY, mode="auto").account() is None
