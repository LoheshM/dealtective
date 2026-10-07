from __future__ import annotations

import pytest

from app import market as mk
from app.market import Market, Offer, classify_store, compute_market, decide, store_key


def offer(store: str, price: float, kind: str = "major", **kw) -> Offer:
    return Offer(store=store, price=price, link=None, title="t", kind=kind, **kw)


def ok_market(ref: float = 1000.0, n: int = 5) -> Market:
    return Market("ok", n, reference=ref, band_low=ref * 0.95, band_high=ref * 1.05, min_price=ref * 0.9,
                  max_price=ref * 1.1)


# --- store_key / classify_store ------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Myntra - MNow", "Myntra"),
        ("amazon.in", "Amazon.in"),
        ("Amazon.in", "Amazon"),
        ("Flipkart | Minutes", "flipkart"),
        ("Croma – Online", "Croma"),
        ("Reliance Digital", "reliancedigital"),
        ("Tata CLiQ", "tatacliq.com"),
        ("  Vijay Sales  ", "Vijay Sales"),
        ("Poorvika.co.in", "Poorvika"),
    ],
)
def test_store_key_equal(a, b):
    assert store_key(a) == store_key(b)


def test_store_key_distinct():
    assert store_key("Flipkart") != store_key("Shopsy By Flipkart")
    assert store_key("Myntra") != store_key("Amazon.in")
    assert store_key("") == ""
    assert store_key(None) == ""


@pytest.mark.parametrize(
    ("store", "brand", "kind"),
    [
        ("boAt", "boAt", "official"),
        ("boAt Lifestyle", "boAt", "official"),
        ("boat-lifestyle.com", "boAt", "official"),
        ("Philips Domestic Appliances", "Philips", "official"),
        ("Samsung", "Samsung", "official"),
        ("Samsung", None, "major"),  # without a brand, brand stores sit on the major list
        ("Zepto", "boAt", "quick"),
        ("Blinkit", None, "quick"),
        ("Swiggy Instamart", None, "quick"),
        ("Flipkart Minutes", None, "quick"),
        ("BigBasket", None, "quick"),
        ("Flipkart", "boAt", "major"),
        ("Amazon.in", "boAt", "major"),
        ("Croma", None, "major"),
        ("Reliance Digital", None, "major"),
        ("Myntra - MNow", None, "major"),
        ("Tata CLiQ", None, "major"),
        ("JioMart Electronics", None, "major"),
        ("Shopy Vision", None, "other"),  # contains 'hp' as a substring, must not be HP
        ("Shopy Vision", "boAt", "other"),
        ("Zop", "boAt", "other"),
        ("Gadgets Now", None, "other"),
        ("Happy Store", None, "other"),
        ("", None, "other"),
    ],
)
def test_classify_store(store, brand, kind):
    assert classify_store(store, brand) == kind


def test_classify_store_hp_whole_word():
    assert classify_store("HP World", None) == "major"
    assert classify_store("HP World", "HP") == "official"


def test_is_unavailable():
    assert mk.is_unavailable(["Out of stock online"])
    assert mk.is_unavailable(["Free delivery", "Contact for price"])
    assert mk.is_unavailable(["Currently unavailable"])
    assert not mk.is_unavailable(["Free delivery by Mon"])
    assert not mk.is_unavailable([])
    assert not mk.is_unavailable(None)


def test_percentile():
    vals = [100.0, 200.0, 300.0, 400.0, 500.0]
    assert mk.percentile(vals, 0.0) == 100
    assert mk.percentile(vals, 0.25) == 200
    assert mk.percentile(vals, 0.5) == 300
    assert mk.percentile(vals, 0.75) == 400
    assert mk.percentile(vals, 1.0) == 500
    assert mk.percentile([100.0, 200.0], 0.25) == 125
    assert mk.percentile([42.0], 0.9) == 42
    with pytest.raises(ValueError):
        mk.percentile([], 0.5)


# --- compute_market --------------------------------------------------------------------------


def test_compute_market_trimmed_median_and_band():
    offers = [
        offer("A", 100),  # < 0.5 x raw median (900) -> trimmed away
        offer("B", 800),
        offer("C", 900),
        offer("D", 1000),
        offer("E", 1100),
        offer("F", 5000),  # > 2 x raw median -> trimmed away
    ]
    m = compute_market(offers)
    # raw median of [100, 800, 900, 1000, 1100, 5000] is 950 -> trimmed [800, 900, 1000, 1100]
    assert m.status == "ok"
    assert m.store_count == 4
    assert m.reference == 950
    assert m.band_low == pytest.approx(875)
    assert m.band_high == pytest.approx(1025)
    assert (m.min_price, m.max_price) == (800, 1100)


def test_compute_market_outlier_flags():
    offers = [offer("A", 100), offer("B", 800), offer("C", 900), offer("D", 1000), offer("E", 1100),
              offer("F", 5000), offer("G", 1400)]
    m = compute_market(offers)
    assert m.reference == 1000  # trimmed [800, 900, 1000, 1100, 1400]
    flags = {o.store: o.flag for o in offers}
    assert flags == {"A": "low_outlier", "B": None, "C": None, "D": None, "E": None, "F": "high_outlier",
                     "G": None}


def test_compute_market_outlier_boundaries():
    offers = [offer("A", 1000), offer("B", 1000), offer("C", 1000), offer("Low", 600), offer("High", 1500)]
    m = compute_market(offers)
    assert m.reference == 1000
    # flags are strict inequalities: exactly 0.6x / 1.5x is not an outlier
    assert {o.store: o.flag for o in offers}["Low"] is None
    assert {o.store: o.flag for o in offers}["High"] is None
    lo = offer("Lower", 599)
    compute_market([*offers, lo])
    assert lo.flag == "low_outlier"


def test_compute_market_thin_when_fewer_than_min_stores():
    assert mk.MIN_STORES == 3
    m = compute_market([offer("A", 900), offer("B", 1000)], google_range=(800.0, 1200.0))
    assert m.status == "thin"
    assert m.store_count == 2
    assert m.reference is None
    assert (m.min_price, m.max_price) == (900, 1000)
    assert m.google_range == (800.0, 1200.0)


def test_compute_market_thin_after_trimming():
    # 3 stores, but two are far from the median and get trimmed
    m = compute_market([offer("A", 10), offer("B", 1000), offer("C", 100000)])
    assert m.status == "thin"
    assert m.store_count == 1


def test_compute_market_empty():
    m = compute_market([])
    assert m.status == "thin"
    assert m.store_count == 0
    assert m.min_price is None


def test_compute_market_excludes_anchor_and_not_in_reference():
    anchor = offer("Amazon.in (this listing)", 100, is_anchor=True)
    own = offer("Amazon.in", 100, in_reference=False)
    others = [offer("B", 1000), offer("C", 1000), offer("D", 1000)]
    m = compute_market([anchor, own, *others])
    assert m.status == "ok"
    assert m.store_count == 3
    assert m.reference == 1000
    # ...but outlier flags still apply to every offer, including the anchor
    assert anchor.flag == "low_outlier"
    assert own.flag == "low_outlier"


def test_compute_market_only_anchor_and_own_store_is_thin():
    m = compute_market([offer("x", 500, is_anchor=True), offer("Amazon.in", 500, in_reference=False),
                        offer("B", 1000)])
    assert m.status == "thin"
    assert m.store_count == 1


def test_compute_market_excludes_unavailable_and_zero_prices():
    offers = [offer("B", 1000), offer("C", 1000), offer("D", 1000), offer("E", 1, available=False),
              offer("F", 0)]
    m = compute_market(offers)
    assert m.store_count == 3
    assert m.reference == 1000


def test_compute_market_one_price_per_store_lowest_wins():
    offers = [
        offer("Myntra", 1200),
        offer("Myntra - MNow", 1000),  # same retailer as Myntra, lower -> kept
        offer("amazon.in", 900),
        offer("Amazon.in", 1500),
        offer("Croma", 1100),
    ]
    m = compute_market(offers)
    assert m.store_count == 3  # myntra, amazon, croma
    assert m.reference == 1000
    assert (m.min_price, m.max_price) == (900, 1100)


def test_market_public_rounds():
    m = Market("ok", 3, reference=799.4, band_low=799.6, band_high=949.5, min_price=1.0, max_price=2.0,
               google_range=(999.0, 1199.0))
    pub = m.public()
    assert pub["reference"] == 799
    assert pub["band_low"] == 800
    assert pub["google_range"] == [999.0, 1199.0]
    assert Market("thin", 0).public()["reference"] is None


# --- decide ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("deal", "expected"),
    [
        (500, "unusually_low"),
        (900, "good_deal"),
        (930, "good_deal"),  # exactly 0.93 x reference
        (931, "fair_price"),
        (1000, "fair_price"),
        (1069, "fair_price"),
        (1070, "above_market"),  # exactly 1.07 x reference
        (2000, "above_market"),
    ],
)
def test_decide_label_thresholds(deal, expected):
    assert decide(ok_market(1000), deal, None, []).label == expected


def test_decide_not_enough_data_when_thin():
    v = decide(Market("thin", 2), 799, 3990, [])
    assert v.label == "not_enough_data"
    assert v.real_saving is None
    assert v.mrp_theatre is False
    assert v.claimed_discount == pytest.approx(0.7997, abs=1e-4)
    assert v.headline == "Not enough clean market data to judge this price — the listing claims 80% off"


def test_decide_not_enough_data_without_claim():
    v = decide(Market("thin", 0), None, None, [])
    assert v.label == "not_enough_data"
    assert v.headline == "Not enough clean market data to judge this price"


def test_decide_no_deal_price():
    v = decide(ok_market(799, n=4), None, 3990, [])
    assert v.label == "no_deal_price"
    assert v.deal_price is None
    assert v.mrp_multiple == pytest.approx(3990 / 799)
    assert v.headline == "Market price today is about ₹799 across 4 stores"


def test_decide_claimed_discount_and_mrp_multiple():
    v = decide(ok_market(800), 800, 4000, [])
    assert v.claimed_discount == pytest.approx(0.8)
    assert v.mrp_multiple == pytest.approx(5.0)
    assert v.real_saving == 0
    assert v.real_saving_abs == 0


@pytest.mark.parametrize("mrp", [None, 0, 800, 700])
def test_decide_no_claim_when_mrp_missing_or_not_above_deal(mrp):
    v = decide(ok_market(800), 800, mrp, [])
    assert v.claimed_discount is None
    assert v.mrp_theatre is False


def test_mrp_theatre_true_for_inflated_mrp():
    # the boAt case: 80% claimed off a ₹3,990 MRP, everyone sells at ₹799
    v = decide(ok_market(799), 799, 3990, [])
    assert v.label == "fair_price"
    assert v.mrp_theatre is True
    assert v.public()["mrp_multiple"] == 5.0


def test_mrp_theatre_needs_claimed_discount_of_30_percent():
    # MRP 1.5x market but claimed discount 25% -> no theatre
    v = decide(ok_market(1000), 1125, 1500, [])
    assert v.claimed_discount == pytest.approx(0.25)
    assert v.mrp_theatre is False


def test_mrp_theatre_needs_mrp_at_least_1_5x_reference():
    # claimed 40% off, but the MRP is only 1.4x what the market charges -> real discount
    v = decide(ok_market(1000), 840, 1400, [])
    assert v.claimed_discount == pytest.approx(0.4)
    assert v.mrp_multiple == pytest.approx(1.4)
    assert v.mrp_theatre is False


def test_mrp_theatre_boundaries_inclusive():
    v = decide(ok_market(1000), 1050, 1500, [])  # claimed exactly 30%, MRP exactly 1.5x
    assert v.claimed_discount == pytest.approx(0.30)
    assert v.mrp_theatre is True


def test_best_trusted_and_cheapest_any():
    offers = [
        offer("Shady", 300, kind="major", flag="low_outlier"),  # outlier -> excluded everywhere
        offer("Gone", 500, kind="official", available=False),  # unavailable -> excluded
        offer("RandomShop", 700, kind="other"),  # cheapest, but not trusted
        offer("Zepto", 760, kind="quick"),
        offer("Croma", 800, kind="major"),
        offer("boAt", 820, kind="official"),
        offer("Free", 0, kind="major"),
    ]
    v = decide(ok_market(800), 800, None, offers)
    assert v.best_trusted["store"] == "Zepto"
    assert v.cheapest_any["store"] == "RandomShop"


def test_best_trusted_none_when_only_other_stores():
    v = decide(ok_market(800), 800, None, [offer("RandomShop", 700, kind="other")])
    assert v.best_trusted is None
    assert v.cheapest_any["store"] == "RandomShop"


def test_best_trusted_present_even_when_thin():
    v = decide(Market("thin", 1), 800, None, [offer("Croma", 790)])
    assert v.label == "not_enough_data"
    assert v.best_trusted["store"] == "Croma"


# --- headline --------------------------------------------------------------------------------


def test_headline_positive_saving_with_claim():
    v = decide(ok_market(1000), 800, 2000, [])
    assert v.real_saving == pytest.approx(0.2)
    assert v.real_saving_abs == pytest.approx(200)
    assert v.headline == "Claimed 60% off → real saving vs market ₹200 (20%)"


def test_headline_positive_saving_without_claim_is_capitalised():
    v = decide(ok_market(1000), 800, None, [])
    assert v.headline == "Real saving vs market ₹200 (20%)"


def test_headline_negative_saving():
    v = decide(ok_market(1000), 1250, 1500, [])
    assert v.real_saving == pytest.approx(-0.25)
    assert v.headline == "Claimed 17% off → ₹250 (25%) above the market price"
    v2 = decide(ok_market(1000), 1250, None, [])
    assert v2.headline == "₹250 (25%) above the market price"


def test_headline_zero_saving():
    v = decide(ok_market(799), 799, 3990, [])
    assert v.headline == "Claimed 80% off → same as the market price"
    v2 = decide(ok_market(1000), 1004, None, [])  # within ±0.5% counts as the same
    assert v2.headline == "Same as the market price"


def test_headline_uses_indian_rupee_thousands_separator():
    v = decide(ok_market(30000), 25000, None, [])
    assert v.headline == "Real saving vs market ₹5,000 (17%)"


def test_verdict_public_rounding():
    v = decide(ok_market(799), 799, 3990, [offer("Croma", 799)])
    pub = v.public()
    assert pub["label"] == "fair_price"
    assert pub["deal_price"] == 799
    assert pub["mrp"] == 3990
    assert pub["claimed_discount"] == 0.7997
    assert pub["real_saving"] == 0
    assert pub["mrp_multiple"] == 5.0
    assert pub["mrp_theatre"] is True
    assert pub["best_trusted"]["store"] == "Croma"


def test_offer_public_has_all_fields():
    pub = offer("Croma", 799, notes=["x"], is_anchor=True).public()
    assert pub["is_anchor"] is True
    assert pub["in_reference"] is True
    assert pub["notes"] == ["x"]
    assert {"store", "price", "link", "title", "kind", "rating", "reviews", "logo", "origin", "available",
            "flag"} <= pub.keys()
