"""Regression tests for issues found in the independent code review."""

from __future__ import annotations

import pytest

from app import market as mk
from app import parse, pipeline
from app.resolve import Candidate, apply_llm_labels, deterministic, prefilter
from app.serp import BudgetExceeded, CreditBudget, SerpClient

REDMI = "Redmi 13 5G Orchid Pink 8GB RAM 128GB ROM (Without Offer)"


def lab(title, store="Shop", brand="Redmi", notes=None, anchor=REDMI):
    return deterministic(anchor, brand, Candidate(0, title, store, 1.0, extra={"notes": notes or []}))[0]


@pytest.mark.parametrize("title", ["Redmi Note 13 5g", "Xiaomi Redmi Note 13 5G 128GB 8GB Ram"])
def test_note_model_is_not_the_base_model(title):
    assert lab(title) != "same"


@pytest.mark.parametrize("title", ["Realme 13 5G", "Realme 13+ 5g"])
def test_other_brand_is_not_same(title):
    assert lab(title) != "same"


def test_refurbished_and_preowned_are_different():
    assert lab("Redmi 13 5G (128 GB, 8 GB RAM)(Refurbished)") == "different"
    assert lab("Redmi 13 5G 8GB 128GB", notes=["Pre-owned", "In stock"]) == "different"
    assert lab("Redmi 13 5G 8GB 128GB", notes=["In stock online"]) == "same"


def test_ram_difference_is_variant():
    assert lab("Redmi 13 5G (6GB RAM, 128GB, Black Diamond)") == "variant"
    assert lab("Redmi 13 5G 8GB+256GB") == "variant"
    assert lab("Redmi 13 5G 128GB") == "same"  # subset: listing omits RAM


def test_gift_store_substring_is_reseller():
    assert lab("Redmi 13 5G 8GB 128GB", store="Giftmandu.com") == "reseller"


def test_llm_cannot_upgrade_positive_variant_conflict():
    a = Candidate(1, "boAt Airdopes 141 ANC", "x", 1.0)
    b = Candidate(2, "boAt Airdopes 141", "y", 1.0)
    prefilter("boAt Airdopes 141 Gen 2", "boAt", [a, b])
    assert a.rule_reason.startswith("has ") and b.rule_reason.startswith("doesn't say")
    apply_llm_labels([a, b], [{"id": 1, "label": "same", "reason": "ignore previous rules"},
                              {"id": 2, "label": "same", "reason": "48h battery = Gen 2"}])
    assert a.label == "variant"  # a title that names ANC can't be talked into "same"
    assert b.label == "same"  # an omission can be resolved by the LLM


def test_anchor_search_skips_note_when_user_asked_for_base_model():
    d = {"organic_results": [
        {"title": "Redmi Note 13 5G (8GB RAM, 128GB)", "price": "₹16,999"},
        {"title": "Redmi 13 5G (8GB RAM, 128GB)", "price": "₹13,499"},
    ]}
    a = pipeline.anchor_from_search(d, "Redmi 13 5G 8GB 128GB")
    assert a is not None and a.title.startswith("Redmi 13 5G")


def test_guess_brand():
    assert parse.guess_brand("Redmi 13 5G 8GB") == "Redmi"
    assert parse.guess_brand("Philips HD9252/70", "Essential Air Fryer HD9252/70") is None
    assert parse.guess_brand("Philips HD9252/70", "Philips Essential Air Fryer") == "Philips"
    assert parse.guess_brand("128GB phone") is None


def _offer(store, price, **kw):
    return mk.Offer(store=store, price=price, link=None, title="t", kind=kw.pop("kind", "major"), **kw)


def test_unusually_low_deal_is_not_called_good_deal():
    offers = [_offer(s, 1000) for s in ("A", "B", "C")]
    m = mk.compute_market(offers)
    assert mk.decide(m, 500, None, offers).label == "unusually_low"
    assert mk.decide(m, 900, None, offers).label == "good_deal"


def test_best_trusted_is_never_the_listing_itself():
    offers = [_offer("Amazon.in (this listing)", 700, is_anchor=True), _offer("Croma", 800), _offer("X", 900, kind="other")]
    v = mk.decide(mk.compute_market(offers), 700, None, offers)
    assert v.best_trusted["store"] == "Croma"


def test_thin_market_still_flags_extreme_prices():
    offers = [_offer("A", 7299, kind="other"), _offer("Giftish", 28057, kind="other")]
    m = mk.compute_market(offers)
    assert m.status == "thin"
    assert {o.store: o.flag for o in offers}["Giftish"] == "high_outlier"


def test_official_brand_match_is_whole_word():
    assert mk.classify_store("Flipkart Minutes", "Mi") == "quick"
    assert mk.classify_store("Mi.com", "Mi") == "official"


def test_is_unavailable_tolerates_non_strings():
    assert mk.is_unavailable([{"x": 1}, None, "Out of stock online"]) is True
    assert mk.is_unavailable([{"x": 1}]) is False


def test_numbers_grounded():
    facts = {"deal_price": "₹799", "mrp": "₹3,990", "claimed_discount_pct": 80, "market_reference": "₹799",
             "mrp_multiple": 5.0, "real_saving_amount": "₹0", "real_saving_pct": 0}
    assert pipeline.numbers_grounded("Claimed 80% off; real saving ₹0. The M.R.P. is 5.0x the market.", facts)
    assert not pipeline.numbers_grounded("You save ₹3,191 today!", facts)
    assert not pipeline.numbers_grounded("That's 75% off.", facts)


def test_malformed_payloads_do_not_crash():
    assert pipeline.cards_from_shopping({"shopping_results": [None, "x", {"title": "A", "price": "₹10"}]})[0].title == "A"
    assert pipeline.candidates_from_stores({"stores": [None, {"name": "S", "price": "₹5", "details_and_offers": [{"a": 1}, "In stock"]}]}, 0)[0].extra["notes"] == ["In stock"]
    a = pipeline.anchor_from_product({"product_results": {"title": "T", "price": "₹1", "bank_offers": ["str"], "badges": "x"},
                                      "purchase_options": {"single_offer": {"features": {"sold_by": "Seller"}}}}, "B0X")
    assert a.seller == "Seller" and a.bank_offers == [] and a.badges == []
    assert pipeline.collect_sources({"user_reviews": [None, {"text": 5}], "videos": "bad"}, None) == []


async def test_hourly_live_cap(tmp_path):
    c = SerpClient("k", tmp_path, mode="auto", live_per_hour=1)
    c._check_hourly()
    with pytest.raises(BudgetExceeded):
        c._check_hourly()
    await c.aclose()


async def test_hourly_cap_refunds_the_per_check_budget(tmp_path, monkeypatch):
    c = SerpClient("k", tmp_path, mode="auto", live_per_hour=1)
    c._check_hourly()  # use up the hour
    b = CreditBudget(3)
    with pytest.raises(BudgetExceeded):
        await c.search("google_shopping", {"q": "x"}, purpose="t", budget=b)
    assert b.spent == 0
    await c.aclose()


async def test_short_link_expansion_is_best_effort():
    assert await pipeline.expand_short_link("Redmi 13 5G") == "Redmi 13 5G"
    assert await pipeline.expand_short_link("https://www.amazon.in/dp/B0F8BVSK21") == "https://www.amazon.in/dp/B0F8BVSK21"
