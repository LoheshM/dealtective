"""Pure helpers in the pipeline: extraction, offer building, narration fallbacks, LLM cache."""

from __future__ import annotations

import json

import pytest

from app import market as mk
from app import pipeline, resolve
from app.llm import LLM, NARRATE_SCHEMA, NARRATE_SYSTEM


def amazon_product(**pr) -> dict:
    base = {"title": "boAt Airdopes 141 Gen 2, ENx", "brand": "Visit the boAt Store", "price": "₹799",
            "old_price": "80% off₹3,990", "thumbnail": "https://img", "rating": 4.1, "reviews": 1234}
    return {
        "product_results": {**base, **pr},
        "purchase_options": {"single_offer": {"price": "₹799", "features": {"sold_by": {"text": "Seller X"}}}},
        "reviews_information": {"summary": {"text": "Customers like the sound."}},
    }


def test_anchor_from_product_parses_trap_and_brand():
    a = pipeline.anchor_from_product(amazon_product(), "B0F8BVSK21")
    assert a.price == 799 and a.mrp == 3990
    assert a.brand == "boAt"
    assert a.seller == "Seller X"
    assert a.link == "https://www.amazon.in/dp/B0F8BVSK21"
    assert a.review_summary == "Customers like the sound."
    assert a.public()["asin"] == "B0F8BVSK21"


def test_anchor_from_product_price_falls_back_to_offer():
    a = pipeline.anchor_from_product(amazon_product(price=None), "B0F8BVSK21")
    assert a.price == 799


def test_anchor_from_product_without_title_is_none():
    assert pipeline.anchor_from_product({"product_results": {}}, "B0F8BVSK21") is None
    assert pipeline.anchor_from_product({}, "B0F8BVSK21") is None


def test_anchor_from_search_requires_model_and_skips_accessories():
    d = {"organic_results": [
        {"title": "Case for Redmi 13 5G", "price": "₹199"},
        {"title": "Redmi 13C 5G 8GB 128GB", "price": "₹9,999"},
        {"title": "Redmi 13 5G Orchid Pink 8GB RAM 128GB ROM", "price": "₹13,999", "old_price": "₹19,999"},
        {"title": "Redmi 13 5G no price"},
    ]}
    a = pipeline.anchor_from_search(d, "Redmi 13 5G 8GB 128GB")
    assert a is not None
    assert a.title.startswith("Redmi 13 5G Orchid Pink")
    assert (a.price, a.mrp) == (13999, 19999)


def test_anchor_from_search_slash_model():
    d = {"organic_results": [
        {"title": "Philips Air Fryer HD9252/70, uses up to 90% less fat", "price": "₹7,780"},
        {"title": "Philips Air Fryer HD9252/90 Digital", "price": "₹6,799"},
    ]}
    a = pipeline.anchor_from_search(d, "Philips HD9252/90 air fryer")
    assert a is not None and "HD9252/90" in a.title


def test_anchor_from_search_low_overlap_is_none():
    d = {"organic_results": [{"title": "Totally unrelated kettle", "price": "₹999"}]}
    assert pipeline.anchor_from_search(d, "Redmi 13 5G") is None
    assert pipeline.anchor_from_search({}, "Redmi 13 5G") is None


def test_cards_from_shopping_dedupes_and_skips_unpriced():
    g = {"shopping_results": [
        {"title": "A", "source": "Croma", "price": "₹799", "immersive_product_page_token": "tok",
         "old_price": "₹3,990", "source_icon": "logo"},
        {"title": "A", "source": "Croma", "price": "₹799"},  # duplicate
        {"title": "B", "source": "Flipkart"},  # no price
        {"source": "Zepto", "price": "₹1"},  # no title
        {"title": "C", "price": "₹900"},
    ]}
    cards = pipeline.cards_from_shopping(g, start_id=100)
    assert [(c.id, c.title, c.store, c.price) for c in cards] == [(100, "A", "Croma", 799), (104, "C", "?", 900)]
    assert cards[0].extra["page_token"] == "tok"
    assert cards[0].extra["old_price"] == 3990
    assert cards[0].origin == "google_shopping"


def test_candidates_from_stores():
    pr = {"title": "boAt Airdopes 141 Gen 2", "stores": [
        {"name": "Flipkart", "price": "₹799", "details_and_offers": ["Free delivery"]},
        {"name": "Zop", "extracted_price": 849},
        {"name": "NoPrice"},
        {"title": "Own title", "price": "₹900"},
    ]}
    cands = pipeline.candidates_from_stores(pr, start_id=1000)
    assert [(c.id, c.store, c.price, c.title) for c in cands] == [
        (1000, "Flipkart", 799, "boAt Airdopes 141 Gen 2"),
        (1001, "Zop", 849, "boAt Airdopes 141 Gen 2"),
        (1003, "?", 900, "Own title"),
    ]
    assert cands[0].extra["notes"] == ["Free delivery"]


def _cand(cid, store, price, label="same", **extra):
    c = resolve.Candidate(id=cid, title="t", store=store, price=price, extra=extra)
    c.label = label
    return c


def test_choose_card_prefers_official_then_reviews():
    same = [
        _cand(1, "Random", 700, page_token="a", reviews=9999),
        _cand(2, "Flipkart", 799, page_token="b", reviews=10),
        _cand(3, "Croma", 799, page_token="c", reviews=500),
        _cand(4, "boAt", 799, page_token=None),  # no token -> cannot expand
    ]
    assert pipeline.choose_card(same, "boAt").id == 3
    assert pipeline.choose_card([*same, _cand(5, "boAt Lifestyle", 799, page_token="d")], "boAt").id == 5
    assert pipeline.choose_card([_cand(1, "x", 1, page_token=None)], None) is None
    assert pipeline.choose_card([], None) is None


def test_pick_reference_card():
    cards = [_cand(1, "s", 1), _cand(2, "s", 1)]
    cards[0].title, cards[1].title = "Something else", "Redmi 13 5G 8GB 128GB"
    assert pipeline.pick_reference_card(cards, "Redmi 13 5G 8GB 128GB").id == 2
    assert pipeline.pick_reference_card(cards, "Sony WH-1000XM5") is None


def test_build_offers():
    anchor = pipeline.Anchor(source="amazon", title="X", brand="boAt", price=799, mrp=3990, link="l", image=None,
                             seller="Seller X")
    stores = [
        _cand(1000, "Amazon.in", 799),
        _cand(1001, "Zepto", 820),
        _cand(1002, "Flipkart", 799, notes=["Out of stock"]),
        _cand(1003, "Variant Shop", 500, label="variant"),
        _cand(1004, "Free", 0),
    ]
    cards = [_cand(1, "Flipkart", 650), _cand(2, "boAt", 799)]
    offers = pipeline.build_offers(anchor, cards, stores, "boAt")
    by = {o.store: o for o in offers}
    assert set(by) == {"Amazon.in", "Zepto", "Flipkart", "boAt", "Amazon.in (this listing)"}
    # one row per retailer: an in-stock listing beats an out-of-stock one, then the lowest price wins
    assert by["Flipkart"].price == 650
    assert by["Flipkart"].available is True
    assert by["Zepto"].kind == "quick" and "Price may vary by pincode" in by["Zepto"].notes
    assert by["boAt"].kind == "official"
    assert by["Amazon.in"].in_reference is False
    assert by["Amazon.in (this listing)"].is_anchor and by["Amazon.in (this listing)"].notes == ["Sold by Seller X"]


def test_build_offers_without_anchor():
    offers = pipeline.build_offers(None, [_cand(1, "Amazon.in", 799)], [], None)
    assert len(offers) == 1 and offers[0].in_reference is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("₹999 – ₹1,199", (999.0, 1199.0)),
        ("₹1,199 - ₹999", (999.0, 1199.0)),
        ("₹999", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_range(raw, expected):
    assert pipeline.parse_range(raw) == expected


def test_collect_sources_ids_and_limits():
    pr = {
        "user_reviews": [{"text": f"review {i}", "title": "t"} for i in range(8)] + [{"text": "  "}],
        "discussions_and_forums": [{"title": "thread", "date": "2025", "comments": "12 comments"}],
        "videos": [{"title": "unboxing", "channel": "Tech"}],
    }
    anchor = pipeline.Anchor(source="amazon", title="X", brand=None, price=1, mrp=None, link="l", image=None,
                             review_summary="Good")
    src = pipeline.collect_sources(pr, anchor)
    assert [s["type"] for s in src] == ["review"] * 6 + ["forum", "video", "store_summary"]
    assert [s["id"] for s in src] == list(range(9))
    assert src[6]["meta"] == "2025 · 12 comments"
    assert pipeline.collect_sources({}, None) == []


def test_template_summary():
    m = mk.Market("ok", 3, reference=799)
    v = mk.decide(m, 799, 3990, [])
    s = pipeline.template_summary(m, v)
    assert "same as the market price" in s
    assert "about ₹799" in s
    assert "5.0×" in s
    thin = mk.Market("thin", 1)
    assert "couldn't find at least three" in pipeline.template_summary(thin, mk.decide(thin, 799, None, []))


async def test_narrate_falls_back_to_template_without_llm(tmp_path):
    llm = LLM(None, "m", tmp_path, None, replay=True)
    m = mk.Market("ok", 3, reference=799)
    v = mk.decide(m, 799, 3990, [])
    out = await pipeline.narrate(llm, m, v, None, [])
    assert out["generated_by"] == "template"
    assert out["voices"] == []


async def test_narrate_uses_cached_llm_and_drops_bad_source_ids(tmp_path, monkeypatch):
    llm = LLM(None, "m", tmp_path, None, replay=True)
    captured = {}

    async def fake_json(system, user, schema, name):
        captured["key"] = llm._key(system, user, schema)
        return None, "unavailable"

    m = mk.Market("ok", 3, reference=799)
    v = mk.decide(m, 799, 3990, [])
    sources = [{"id": 0, "type": "review", "title": "t", "text": "great"}]
    monkeypatch.setattr(llm, "json", fake_json)
    await pipeline.narrate(llm, m, v, None, sources)
    monkeypatch.undo()

    # pre-seed the cache under the exact key the pipeline will compute
    (tmp_path / "llm" / f"{captured['key']}.json").write_text(json.dumps({
        "summary": "Sold at the market price.",
        "voices": [
            {"point": "Good sound", "tone": "positive", "source_ids": [0, 7]},
            {"point": "Unsupported", "tone": "negative", "source_ids": [42]},
        ],
    }), encoding="utf-8")
    out = await pipeline.narrate(llm, m, v, None, sources)
    assert out["generated_by"] == "llm"
    assert out["summary"] == "Sold at the market price."
    assert out["voices"] == [{"point": "Good sound", "tone": "positive", "source_ids": [0]}]


async def test_llm_resolve_applies_cached_labels(tmp_path):
    llm = LLM(None, "m", tmp_path, None, replay=True)
    cands = [_cand(1, "Croma", 799), _cand(2, "Zop", 799, label="variant"), _cand(3, "X", 1, label="accessory")]
    for c in cands:
        c.reason = "rule"
    prompt = resolve.resolve_prompt("anchor", cands[:2])  # only same/variant are sent
    key = llm._key(resolve.RESOLVE_SYSTEM, prompt, resolve.RESOLVE_SCHEMA)
    (tmp_path / "llm" / f"{key}.json").write_text(json.dumps({"labels": [
        {"id": 1, "label": "different", "reason": "refurbished"},
        {"id": 2, "label": "same", "reason": "colour only"},
        {"id": 3, "label": "same", "reason": "should be ignored: not sent"},
    ]}), encoding="utf-8")
    await pipeline.llm_resolve(llm, "anchor", cands)
    assert [c.label for c in cands] == ["different", "same", "accessory"]


# --- LLM wrapper -------------------------------------------------------------------------------


async def test_llm_without_key_is_unavailable(tmp_path):
    llm = LLM(None, "gpt-x", tmp_path, None, replay=False)
    assert llm.available is False
    assert await llm.json(NARRATE_SYSTEM, "u", NARRATE_SCHEMA, "narrate") == (None, "unavailable")


async def test_llm_replay_with_key_never_creates_client(tmp_path):
    llm = LLM("sk-test-not-real", "gpt-x", tmp_path, None, replay=True)
    assert llm.available is False


async def test_llm_reads_cache_then_fixtures(tmp_path):
    fx = tmp_path / "fx"
    (fx / "llm").mkdir(parents=True)
    llm = LLM(None, "gpt-x", tmp_path / "cache", fx, replay=True)
    key = llm._key("s", "u", {"type": "object"})
    (fx / "llm" / f"{key}.json").write_text(json.dumps({"from": "fixture"}), encoding="utf-8")
    assert await llm.json("s", "u", {"type": "object"}, "n") == ({"from": "fixture"}, "cache")
    (tmp_path / "cache" / "llm" / f"{key}.json").write_text(json.dumps({"from": "cache"}), encoding="utf-8")
    assert await llm.json("s", "u", {"type": "object"}, "n") == ({"from": "cache"}, "cache")
    assert key in llm.used_keys


def test_llm_key_depends_on_model_and_inputs(tmp_path):
    a = LLM(None, "m1", tmp_path, None, replay=True)
    b = LLM(None, "m2", tmp_path, None, replay=True)
    assert a._key("s", "u", {}) != b._key("s", "u", {})
    assert a._key("s", "u", {}) != a._key("s", "u2", {})
    assert a._key("s", "u", {"x": 1, "y": 2}) == a._key("s", "u", {"y": 2, "x": 1})
