from __future__ import annotations

import pytest

from app import parse
from app.parse import Query, classify, family_query, parse_inr, short_query

# --- classify ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.amazon.in/dp/B0F8BVSK21",
        "https://www.amazon.in/dp/B0F8BVSK21/",
        "https://www.amazon.in/dp/B0F8BVSK21?th=1&psc=1",
        "https://www.amazon.in/dp/B0F8BVSK21/ref=sr_1_3?crid=2X&keywords=airdopes&sr=8-3",
        "https://www.amazon.in/boAt-Airdopes-141-Gen-2/dp/B0F8BVSK21/ref=sr_1_1",
        "https://www.amazon.in/gp/product/B0F8BVSK21",
        "https://www.amazon.in/gp/product/B0F8BVSK21/ref=ppx_yo_dt_b_asin_title",
        "https://www.amazon.in/gp/aw/d/B0F8BVSK21",
        "www.amazon.in/dp/B0F8BVSK21",
        "amazon.in/dp/b0f8bvsk21",
        "  https://www.amazon.in/dp/B0F8BVSK21#reviews  ",
    ],
)
def test_classify_amazon_urls(raw):
    q = classify(raw)
    assert q.kind == "amazon_asin"
    assert q.value == "B0F8BVSK21"
    assert q.raw == raw


@pytest.mark.parametrize("raw", ["B0F8BVSK21", "b0f8bvsk21", "  B0F8BVSK21 "])
def test_classify_bare_asin(raw):
    assert classify(raw) == Query("amazon_asin", "B0F8BVSK21", raw)


def test_classify_ten_chars_not_starting_b0_is_text():
    assert classify("ABCDEFGHIJ").kind == "text"


@pytest.mark.parametrize("raw", ["https://amzn.in/d/abc123X", "https://amzn.eu/d/3kXyz9Q", "amzn.in/d/abc123X"])
def test_classify_amzn_short_link_without_asin_is_error(raw):
    with pytest.raises(ValueError, match="could not read a product"):
        classify(raw)


def test_classify_amazon_url_without_asin_falls_back_to_slug():
    q = classify("https://www.amazon.in/boAt-Airdopes-141-Gen-2/s?k=airdopes")
    assert q.kind == "url_slug"
    assert q.value == "boAt Airdopes 141 Gen 2"


def test_classify_flipkart_url_is_slug_text():
    q = classify("https://www.flipkart.com/boat-airdopes-141-gen-2/p/itm8a7b9c0d1e2f3?pid=ACCH12345&lid=LST")
    assert q == Query("url_slug", "boat airdopes 141 gen 2", q.raw)


def test_classify_croma_url_is_slug_text():
    q = classify("https://www.croma.com/philips-hd9252-90-air-fryer/p/245678")
    assert q.kind == "url_slug"
    assert q.value == "philips hd9252 90 air fryer"


def test_classify_url_with_no_readable_slug_is_error():
    with pytest.raises(ValueError):
        classify("https://example.com/p/abcdef123")


def test_classify_plain_text_collapses_whitespace():
    q = classify("  Redmi 13   5G\t8GB  128GB ")
    assert q.kind == "text"
    assert q.value == "Redmi 13 5G 8GB 128GB"


def test_classify_plain_text_truncated_to_150_chars():
    assert len(classify("x" * 400).value) == 150


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_classify_empty_raises(raw):
    with pytest.raises(ValueError, match="empty"):
        classify(raw)


# --- parse_inr -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("₹3,990", 3990.0),
        ("₹1,30,154", 130154.0),  # Indian digit grouping
        ("₹799.00", 799.0),
        ("₹ 799", 799.0),
        ("Rs. 1,299", 1299.0),
        ("Rs 1,299", 1299.0),
        ("rs.499", 499.0),
        ("INR 2,499", 2499.0),
        ("80% off₹3,990", 3990.0),  # the SerpApi extracted_old_price=80 trap
        ("M.R.P.: ₹3,990 (80% off)", 3990.0),
        ("Was ₹1,999 now", 1999.0),
        ("1,299", 1299.0),
        ("799.50", 799.5),
        (799, 799.0),
        (799.5, 799.5),
        (3990.0, 3990.0),
    ],
)
def test_parse_inr_values(value, expected):
    assert parse_inr(value) == expected


def test_parse_inr_trap_never_returns_percentage():
    assert parse_inr("80% off₹3,990") != 80


@pytest.mark.parametrize("value", ["80%", "80% off", None, 0, 0.0, -5, "₹0", "", "free", "Contact for price"])
def test_parse_inr_none(value):
    assert parse_inr(value) is None


# --- titles --------------------------------------------------------------------------------

AIRDOPES = ("boAt Airdopes 141 Gen 2, 4 Mics ENx Tech, 48 Hrs Playback, Fast Charge, Low Latency, IPX4, "
            "v5.4 Bluetooth Earbuds, TWS Ear Buds (Active Black)")
REDMI = "Redmi 13 5G Orchid Pink 8GB RAM 128GB ROM (Without Offer)"


def test_short_query_cuts_at_first_comma():
    assert short_query(AIRDOPES) == "boAt Airdopes 141 Gen 2"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Philips HD9252/90 Air Fryer | 4.1L", "Philips HD9252/90 Air Fryer"),
        ("Redmi 13 5G (Orchid Pink, 8GB)", "Redmi 13 5G"),
        ("Sony WH-1000XM5 - Black", "Sony WH-1000XM5"),
        ("Sony WH-1000XM5 [Renewed]", "Sony WH-1000XM5"),
        ("one two three four five six seven eight nine", "one two three four five six seven"),
        ("", ""),
    ],
)
def test_short_query_separators_and_limit(title, expected):
    assert short_query(title) == expected


def test_short_query_prefixes_missing_brand():
    assert short_query("Airdopes 141 Gen 2, ENx", brand="boAt") == "boAt Airdopes 141 Gen 2"
    assert short_query("boAt Airdopes 141, ENx", brand="BOAT") == "boAt Airdopes 141"


def test_short_query_max_words():
    assert short_query(AIRDOPES, max_words=3) == "boAt Airdopes 141"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("boAt Airdopes 141 Gen 2", "boAt Airdopes 141"),
        (AIRDOPES, "boAt Airdopes 141"),
        (REDMI, "Redmi 13"),
        ("Redmi 13 5G 8GB 128GB", "Redmi 13"),
        ("OnePlus Nord CE4 Lite 5G (Super Silver, 8GB RAM, 128GB)", "OnePlus Nord CE4"),
        ("Apple iPhone 15 Pro Max 256GB Black Titanium", "Apple iPhone 15"),
        ("Samsung Galaxy S24 Ultra 5G (Titanium Gray, 12GB, 256GB Storage)", "Samsung Galaxy S24"),
        ("boAt Airdopes 141 ANC", "boAt Airdopes 141"),
        ("Sony WH-1000XM5 3rd Gen", "Sony WH-1000XM5"),
    ],
)
def test_family_query(title, expected):
    assert family_query(title) == expected


def test_family_query_never_empty():
    # every word is a variant/colour word -> fall back to the head rather than ""
    assert family_query("Pro Max") == "Pro Max"


def test_family_query_with_brand():
    assert family_query("Airdopes 141 Gen 2", brand="boAt") == "boAt Airdopes 141"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("boAt Airdopes 141 Gen 2", "2"),
        ("Airdopes 141 Generation 3", "3"),
        ("Airdopes 141 gen2", "2"),
        ("Echo Dot 5th Gen", "5"),
        ("Echo Dot (3rd gen)", "3"),
        ("Fire TV Stick 2nd Generation", None),  # "2nd Generation" is not in the supported forms
        ("boAt Airdopes 141", None),
        ("", None),
        (None, None),
    ],
)
def test_generation(title, expected):
    assert parse.generation(title) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (REDMI, {"8gb", "128gb"}),
        ("iPhone 15 256 GB", {"256gb"}),
        ("Seagate 2TB HDD", {"2tb"}),
        ("boAt Airdopes 141", set()),
        (None, set()),
    ],
)
def test_storage(title, expected):
    assert parse.storage(title) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("boAt Airdopes 141 Gen 2", {"gen2"}),
        ("boAt Airdopes 141 ANC", {"anc"}),
        ("boAt Airdopes 141", set()),
        ("Redmi Note 13 Pro+ 5G", {"note", "pro", "5g"}),
        ("Samsung Galaxy S24 Ultra 5G", {"ultra", "5g"}),
        ("Apple iPhone 15 Pro Max", {"pro", "max"}),
        ("OnePlus Nord CE4 Lite", {"lite"}),
        ("Echo Dot 5th Gen", {"gen5"}),
    ],
)
def test_variant_markers(title, expected):
    assert parse.variant_markers(title) == expected


def test_tokens_lowercase_alnum():
    assert parse.tokens("boAt Airdopes-141, Gen 2!") == ["boat", "airdopes", "141", "gen", "2"]
    assert parse.tokens(None) == []


def test_core_tokens_drop_stopwords_but_keep_digits():
    toks = parse.core_tokens("boAt Airdopes 141 Gen 2 Wireless Earbuds with Mic, Black")
    assert {"boat", "airdopes", "141", "gen", "2"} <= toks
    assert not toks & {"wireless", "earbuds", "with", "mic", "black"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Visit the boAt Store", "boAt"),
        ("Brand: Philips", "Philips"),
        ("Redmi", "Redmi"),
        ("", None),
        (None, None),
        ("   ", None),
    ],
)
def test_clean_brand(raw, expected):
    assert parse.clean_brand(raw) == expected
