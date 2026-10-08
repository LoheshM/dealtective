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


# --- issues found by cross-checking real products against web prices (Oct 2026) -------------

@pytest.mark.parametrize(("title", "hit"), [
    ("80mm Headphone Cushion Compatible with Rockerz 450, 450 Pro", True),
    ("Charging Cable for Airdopes 141", True),
    ("Soft Mobile Back Cover for OnePlus Nord CE4 Lite 5G", True),
    ("boAt Rockerz 450 Bluetooth On Ear Headphones with Mic, Padded Ear Cushions", False),
    ("boAt Airdopes 141 with Charging Case", False),
    ("Philips Essential HD9252/90 Stand-alone Hot Air fryer", False),
    ("Redmi 13 5G Hawaiian Blue Smartphone | 5000mAH Battery", False),
])
def test_accessory_detection_on_real_titles(title, hit):
    assert bool(parse.accessory_word(title)) is hit


def test_model_number_never_matches_inside_a_decimal_or_spec():
    from app.resolve import _model_numbers, has_model
    assert not has_model("Apple iPhone 17e 256 GB: 15.40 cm (6.1 inch)", "15")
    assert has_model("OnePlus Nord CE 4 Lite 5G", "ce4")
    assert not has_model("OnePlus Nord CE6 Lite", "ce4")
    assert "80mm" not in _model_numbers("80mm Headphone Cushion Rockerz 450")


def test_amazon_title_without_brand_and_spaced_storage_still_matches():
    d = {"organic_results": [{"title": "iPhone 15 (128 GB) - Blue", "price": "₹74,900"}]}
    a = pipeline.anchor_from_search(d, "Apple iPhone 15 128GB")
    assert a is not None and a.price == 74900


def test_anchor_prefers_typical_price_over_first_ranked_seller():
    d = {"organic_results": [
        {"title": "Redmi 13 5G 8GB 128GB Orchid Pink", "price": "₹23,999"},
        {"title": "Redmi 13 5G 8GB 128GB Hawaiian Blue", "price": "₹13,499"},
    ]}
    assert pipeline.anchor_from_search(d, "Redmi 13 5G 8GB 128GB").price == 13499


def test_import_and_nonretail_and_refurb_sellers():
    assert mk.classify_store("desertcart.com.sa", None) == "import"
    assert mk.classify_store("ubuy.co.in", None) == "import"
    assert mk.classify_store("Croma", None) == "major"
    assert mk.classify_store("Meesho", None) == "other"
    assert lab("Redmi 13 5G 8GB 128GB", store="Tradeindia.com") == "reseller"
    assert lab("Redmi 13 5G 8GB 128GB", store="Cashify") == "different"


def test_shopsy_counts_as_flipkart():
    assert mk.store_key("Shopsy By Flipkart") == mk.store_key("Flipkart")


def test_thin_market_flags_use_google_range_not_a_junk_median():
    offers = [_offer("Flipkart", 200), _offer("Cashify", 15699, kind="other")]
    mk.compute_market(offers, google_range=(12847, 19999))
    assert {o.store: o.flag for o in offers} == {"Flipkart": "low_outlier", "Cashify": None}


def test_same_price_and_cheaper_context():
    offers = [_offer("Amazon.in (this listing)", 19989, is_anchor=True), _offer("Amazon.in", 19989, in_reference=False),
              _offer("Flipkart", 19990), _offer("Croma", 22990), _offer("Vijay Sales", 24990), _offer("Reliance Digital", 25990)]
    v = mk.decide(mk.compute_market(offers), 19989, 34990, offers)
    assert v.same_price_at == ["Flipkart"]  # not Amazon's own second row
    assert v.cheaper_at is None
    offers.append(_offer("Tata CLiQ", 18000))
    v = mk.decide(mk.compute_market(offers), 19989, 34990, offers)
    assert v.cheaper_at["store"] == "Tata CLiQ" and v.label == "fair_price"


def test_mojibake_repair_reveals_hindi_for():
    t = parse.fix_mojibake("SPL OnePlus Nord CE4 Lite 5G à¤•à¥‡ à¤²à¤¿à¤ - SPL")
    assert "के लि" in t and parse.accessory_word(t)


# --- non-Amazon store links (Flipkart etc.) are read from Google's index of the page ----------

FLIPKART = ("https://www.flipkart.com/asics-noosa-tri-16-running-shoes-men/p/itmc9ccf049a204d"
            "?pid=SHOHGDNDYHFGQG6D&marketplace=FLIPKART")


def test_store_url_helpers():
    assert parse.store_of(FLIPKART) == ("flipkart.com", "Flipkart")
    assert parse.listing_id(FLIPKART) == "itmc9ccf049a204d"
    assert parse.product_query(parse.slug_text(FLIPKART)) == "asics noosa tri 16"
    assert parse.product_query("apple iphone 15 black 128 gb") == "apple iphone 15 black 128 gb"


def test_snippet_prices():
    flipkart_text = {"snippet": "Asics NOOSA TRI 16 (Multicolor , 14). 51%. 11,999. ₹5,899. Buy at ₹5,309."}
    assert parse.snippet_prices(flipkart_text) == (5899, 11999)
    rich = {"rich_snippet": {"top": {"detected_extensions": {"price": 10499.0, "currency": "₹"}}},
            "snippet": "30%. 14,999. ₹10,499."}
    assert parse.snippet_prices(rich) == (10499, 14999)
    assert parse.snippet_prices({"snippet": "Free shipping on $140 orders"}) == (None, None)


def test_dollar_prices_are_not_rupees():
    assert parse.parse_inr("$140.00") is None
    assert parse.parse_inr("₹140") == 140


def test_possessive_s_is_not_a_variant():
    assert parse.variant_markers("ASICS Men's NOOSA TRI 16") == set()


def test_anchor_from_store_results_prefers_exact_listing():
    d = {"organic_results": [
        {"link": "https://www.flipkart.com/asics-noosa-tri-16/p/itm0af4a0916fb06", "title": "Asics NOOSA TRI 16 Running Shoes For Men",
         "rich_snippet": {"top": {"detected_extensions": {"price": 10499.0, "currency": "₹"}}}, "snippet": "30%. 14,999. ₹10,499."},
        {"link": "https://www.flipkart.com/hi/asics-noosa-tri-16/p/itmc9ccf049a204d", "title": "Asics NOOSA TRI 16 (Multicolor , 14)",
         "snippet": "51%. 11,999. ₹5,899. Buy at ₹5,309. Apply offers"},
    ]}
    a = pipeline.anchor_from_store_results(d, FLIPKART, "asics noosa tri 16 running shoes men", "flipkart.com", "Flipkart")
    assert (a.store, a.price, a.mrp, a.offer_price) == ("Flipkart", 5899, 11999, 5309)
    assert "Google's index" in a.price_note


def test_price_trackers_are_not_stores():
    assert lab("ASICS Noosa Tri 16", store="Price History", anchor="ASICS Noosa Tri 16") == "reseller"
    assert lab("ASICS Noosa Tri 16", store="Buyhatke", anchor="ASICS Noosa Tri 16") == "reseller"


def test_store_listing_excludes_its_own_marketplace():
    a = pipeline.Anchor(source="flipkart", store="Flipkart", title="X", brand=None, price=5899, mrp=11999, link="l", image=None)
    c = Candidate(1, "X", "Flipkart", 6000.0, label="same")
    offers = pipeline.build_offers(a, [], [c], None)
    by = {o.store: o for o in offers}
    assert by["Flipkart"].in_reference is False and by["Flipkart (this listing)"].is_anchor


def test_listing_image_prefers_exact_listing_from_inline_images():
    d = {"inline_images": [
        {"source": "https://www.flipkart.com/x/p/itm0af4a0916fb06", "original": "https://rukminim2.flixcart.com/other.jpg"},
        {"source": "https://www.flipkart.com/hi/x/p/itmc9ccf049a204d", "original": "https://rukminim2.flixcart.com/mine.jpg"},
    ], "organic_results": []}
    assert pipeline.listing_image(d, "itmc9ccf049a204d", "flipkart.com").endswith("mine.jpg")
    assert pipeline.listing_image(d, "itmzzzz", "flipkart.com").endswith("other.jpg")
    assert pipeline.listing_image({"inline_images": [{"original": "http://insecure"}]}, None, "x.com") is None


# --- store links beyond Flipkart (verified with 12 real URLs, Oct 2026) ------------------------

@pytest.mark.parametrize(("url", "store", "lid"), [
    ("https://www.ajio.com/nike-court-royale-2-nn-lace-up-sneakers/p/469098285_white", "AJIO", "469098285"),
    ("https://www.tatacliq.com/jbl-tune-770nc-blue/p-mp000000021644678", "Tata CLiQ", "mp000000021644678"),
    ("https://www.croma.com/jbl-tune-770nc-blue-/p/273407", "Croma", "273407"),
    ("https://www.myntra.com/watches/casio/casio-vintage-a158wa/251023/buy", "Myntra", "251023"),
    ("https://www.nykaa.com/maybelline-fit-me/p/31074", "Nykaa", "31074"),
    ("https://www.vijaysales.com/p/P206834/206834/sony-wh-1000xm5-black", "Vijay Sales", "206834"),
])
def test_store_listing_ids(url, store, lid):
    assert parse.store_of(url)[1] == store
    assert parse.listing_id(url) == lid


def test_url_key_ignores_language_prefix_and_query():
    assert pipeline._url_key("https://www.flipkart.com/hi/x/p/itm1?pid=2") == pipeline._url_key("https://flipkart.com/x/p/itm1/")


def test_store_anchor_ignores_other_domains_and_category_pages():
    url = "https://www.croma.com/jbl-tune-770nc-blue-/p/273407"
    d = {"organic_results": [
        {"link": "https://www.reliancedigital.in/product/jbl-tune-770nc", "title": "JBL Tune 770NC",
         "rich_snippet": {"top": {"detected_extensions": {"price": 5999.0, "currency": "₹"}}}},
        {"link": "https://www.croma.com/c/headphones/1234", "title": "JBL Tune 770NC Blue", "snippet": "₹5,999"},
    ]}
    assert pipeline.anchor_from_store_results(d, url, parse.slug_text(url), "croma.com", "Croma") is None
    d["organic_results"].append({"link": "https://www.croma.com/jbl-tune-770nc-blue-/p/273407", "title": "JBL Tune 770NC",
                                 "rich_snippet": {"top": {"detected_extensions": {"price": 5999.0, "currency": "₹₹"}}}})
    a = pipeline.anchor_from_store_results(d, url, parse.slug_text(url), "croma.com", "Croma")
    assert a.price == 5999 and "closest" not in a.price_note


def test_store_anchor_rejects_near_miss_missing_a_code():
    url = "https://www.ajio.com/nike-court-royale-2-nn-lace-up-sneakers/p/469098285_white"
    d = {"organic_results": [{"link": "https://www.ajio.com/nike-court-royale-2-lace-up-sneakers/p/469175820_brown",
                              "title": "Buy NIKE Court Royale 2 Lace-Up Sneakers Online",
                              "snippet": "Buy NIKE Court Royale 2 Lace-Up Sneakers at 3995 at Ajio.com."}]}
    assert pipeline.anchor_from_store_results(d, url, parse.slug_text(url), "ajio.com", "AJIO") is None


def test_snippet_formats_from_more_stores():
    assert parse.snippet_prices({"snippet": "Buy NIKE Court Royale 2 Lace-Up Sneakers at 3995 at Ajio.com."}) == (3995, None)
    assert parse.snippet_prices({"rich_snippet": {"top": {"detected_extensions": {"price_from": 4798.0, "price_to": 5499.0,
                                                                                    "currency": "₹"}}}})[0] == 4798


def test_anchor_from_offers_uses_the_stores_own_price():
    offers = [_offer("tatacliq.com", 6299), _offer("Amazon.in", 4799)]
    a = pipeline.anchor_from_offers(offers, "https://www.tatacliq.com/x/p-mp1", "Tata CLiQ", "tatacliq.com")
    assert a.price == 6299 and a.store == "Tata CLiQ" and "Shopping listing" in a.price_note
    assert pipeline.anchor_from_offers([_offer("Amazon.in", 4799)], "u", "Croma", "croma.com") is None


def test_d2c_stores_are_known():
    assert "boat-lifestyle.com" in parse.D2C_STORES
    assert parse.store_of("https://www.boat-lifestyle.com/products/airdopes-141") == ("boat-lifestyle.com", "boAt")


def test_llm_provider_selection(monkeypatch):
    from app import config
    for k in ("LLM_PROVIDER", "LLM_MODEL", "OPENAI_MODEL", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    s = config.load_settings()
    assert (s.llm_provider, s.openai_model, s.openai_key) == ("gemini", "gemini-2.5-flash", "g-key")
    assert s.llm_base_url and "generativelanguage" in s.llm_base_url
    monkeypatch.setenv("OPENAI_API_KEY", "o-key")
    s = config.load_settings()
    assert (s.llm_provider, s.openai_model, s.llm_base_url) == ("openai", "gpt-5.4-mini", None)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert config.load_settings().openai_key == "g-key"


def test_replay_uses_recorded_fixture_model(tmp_path):
    from app.llm import LLM
    (tmp_path / "fx" / "llm").mkdir(parents=True)
    (tmp_path / "fx" / "llm" / "MODEL").write_text("gpt-5.4-mini", encoding="utf-8")
    replay = LLM(None, "gemini-2.5-flash", tmp_path / "c", tmp_path / "fx", replay=True)
    live = LLM(None, "gpt-5.4-mini", tmp_path / "c", tmp_path / "fx", replay=False)
    assert replay._key("s", "u", {}) == live._key("s", "u", {})
