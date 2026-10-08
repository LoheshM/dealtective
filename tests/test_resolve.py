from __future__ import annotations

import pytest

from app import resolve
from app.resolve import Candidate, apply_llm_labels, deterministic

AIRDOPES = ("boAt Airdopes 141 Gen 2, 4 Mics ENx Tech, 48 Hrs Playback, Fast Charge, Low Latency, IPX4, "
            "v5.4 Bluetooth Earbuds, TWS Ear Buds (Active Black)")
REDMI_128 = "Redmi 13 5G Orchid Pink 8GB RAM 128GB ROM (Without Offer)"
PHILIPS_90 = "Philips Digital Airfryer HD9252/90, Touch Panel, 4.1L, 100% Food Safe"


def cand(title: str, store: str = "Croma", price: float | None = 799.0, cid: int = 1) -> Candidate:
    return Candidate(id=cid, title=title, store=store, price=price)


def label(anchor: str, title: str, store: str = "Croma", brand: str | None = None) -> str:
    return deterministic(anchor, brand, cand(title, store))[0]


# --- same ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "boAt Airdopes 141 Gen 2 TWS Earbuds",
        "boAt Airdopes 141 Gen 2, 48 Hrs Playback (Bold Black)",
        "boAt Airdopes 141 Gen 2 (Hawaiian Blue)",  # colour difference is still the same product
        "BOAT AIRDOPES 141 GEN 2",
    ],
)
def test_same_product(title):
    lab, reason = deterministic(AIRDOPES, "boAt", cand(title))
    assert lab == "same", reason
    assert reason == "model and variant words match"


def test_colour_difference_stays_same_for_phone():
    assert label(REDMI_128, "Redmi 13 5G Hawaiian Blue 8GB RAM 128GB ROM") == "same"


# --- accessory / reseller -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "word"),
    [
        ("Silicone Case Cover for boAt Airdopes 141 Gen 2", "case"),
        ("Skin for boAt Airdopes 141 Gen 2 - Matte Black", "skin"),
        ("Replacement Ear Tips for boAt Airdopes 141", "replacement"),
        ("Tempered Glass for Redmi 13 5G", "tempered"),
        ("Charging Cable for Airdopes 141", "cable"),
    ],
)
def test_accessory(title, word):
    lab, reason = deterministic(AIRDOPES, "boAt", cand(title))
    assert lab == "accessory"
    assert "accessory" in reason


def test_accessory_word_in_anchor_is_not_a_filter():
    # an anchor that is itself a case must not filter out matching cases
    anchor = "Spigen Rugged Armor Case for iPhone 15"
    assert label(anchor, "Spigen Rugged Armor Case for iPhone 15 - Matte Black") == "same"


@pytest.mark.parametrize(
    ("title", "store"),
    [
        ("boAt Airdopes 141 Gen 2", "Snapmint"),
        ("boAt Airdopes 141 Gen 2 on No Cost EMI", "Croma"),
        ("boAt Airdopes 141 Gen 2", "Giftana gift store"),
        ("boAt Airdopes 141 Gen 2 Corporate Gifting Pack", "Some Store"),
        ("boAt Airdopes 141 Gen 2 bulk order", "Wholesale Hub"),
    ],
)
def test_reseller(title, store):
    lab, reason = deterministic(AIRDOPES, "boAt", cand(title, store))
    assert lab == "reseller"
    assert "reseller" in reason


def test_accessory_checked_before_reseller():
    assert label(AIRDOPES, "Case for boAt Airdopes 141 Gen 2", store="Snapmint") == "accessory"


# --- model numbers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("anchor", "title"),
    [
        ("boAt Airdopes 141", "boAt Airdopes 121v2"),
        ("boAt Airdopes 141 Gen 2", "boAt Airdopes 121v2 TWS Earbuds"),
        ("boAt Airdopes 141", "boAt Airdopes 1410"),
        ("Redmi 13 5G 8GB 128GB", "Redmi 13C 5G 8GB 128GB"),
        ("Sony WH-1000XM5", "Sony WH-1000XM4"),
    ],
)
def test_missing_model_number_is_different(anchor, title):
    lab, reason = deterministic(anchor, None, cand(title))
    assert lab == "different"
    assert reason.startswith("model ")


def test_slash_model_suffix_cannot_be_borrowed_from_elsewhere_in_title():
    # Real Amazon.in title: the '90' in '90% less fat' must not satisfy HD9252/90.
    title = "Essential Air Fryer HD9252/70 with Rapid Air Technology, uses up to 90% less fat, 7 Presets Touch Screen"
    lab, reason = deterministic(PHILIPS_90, "Philips", cand(title))
    # same base model with a different suffix is a variant the resolver may judge, never "same" by rule
    assert lab == "variant"
    assert "HD9252/90" in reason


def test_slash_suffix_mismatch_is_variant_not_same():
    lab, reason = deterministic("Philips HD9252/90 Air Fryer", None, cand("Philips HD9252/70 Air Fryer"))
    assert lab == "variant" and "suffix differs" in reason


def test_slash_model_matches_itself():
    assert label(PHILIPS_90, "Philips HD9252/90 Airfryer 4.1L Black") == "same"


def test_model_numbers_extraction():
    assert resolve._model_numbers("boAt Airdopes 141 Gen 2, 48 Hrs") == {"141"}
    assert resolve._model_numbers(REDMI_128) == {"13"}  # 5G, storage and RAM are not model numbers
    assert resolve._model_numbers("Philips HD9252/90 Air Fryer") == {"hd9252/90"}
    assert resolve._model_numbers("Sony WH-1000XM5") == {"1000xm5"}
    assert resolve._model_numbers("Samsung 4K TV 2025") == set()


@pytest.mark.parametrize(
    ("title", "model", "expected"),
    [
        ("Philips HD9252/90 Airfryer", "hd9252/90", True),
        ("Philips HD9252/70, 90% less fat", "hd9252/90", False),
        ("boAt Airdopes 141", "141", True),
        ("boAt Airdopes 1410", "141", False),
        ("boAt Airdopes 121v2", "141", False),
    ],
)
def test_has_model(title, model, expected):
    assert resolve.has_model(title, model) is expected


# --- variants -------------------------------------------------------------------------------


def test_plain_title_vs_gen2_anchor_is_variant():
    lab, reason = deterministic(AIRDOPES, "boAt", cand("boAt Airdopes 141 TWS Earbuds, 42H Playback"))
    assert lab == "variant"
    assert "doesn't say Gen 2" in reason


def test_gen2_vs_plain_anchor_is_variant():
    lab, reason = deterministic("boAt Airdopes 141", "boAt", cand("boAt Airdopes 141 Gen 2"))
    assert lab == "variant"
    assert "has Gen 2" in reason


def test_different_generation_is_variant():
    assert label("Echo Dot 5th Gen", "Echo Dot 4th Gen") == "variant"


@pytest.mark.parametrize(
    ("anchor", "title", "fragment"),
    [
        ("boAt Airdopes 141", "boAt Airdopes 141 ANC", "has ANC"),
        ("boAt Airdopes 141 ANC", "boAt Airdopes 141", "doesn't say ANC"),
        ("boAt Airdopes 141", "boAt Airdopes 141 Pro", "has PRO"),
        ("boAt Airdopes 141 Pro", "boAt Airdopes 141", "doesn't say PRO"),
        ("Apple iPhone 15 Pro", "Apple iPhone 15 Pro Max", "has MAX"),
        ("OnePlus Nord CE4", "OnePlus Nord CE4 Lite", "has Lite"),
        ("Redmi 13 5G", "Redmi 13 4G", "has 4G"),
    ],
)
def test_variant_words_conflict(anchor, title, fragment):
    lab, reason = deterministic(anchor, None, cand(title))
    assert lab == "variant"
    assert fragment in reason


def test_variant_words_only_read_from_title_head():
    # 'Pro' after the first comma (feature text) must not make it a variant
    assert label("boAt Airdopes 141 Gen 2", "boAt Airdopes 141 Gen 2, Pro sound, ENx") == "same"


@pytest.mark.parametrize(
    ("anchor", "title"),
    [
        ("Redmi 13 5G 128GB", "Redmi 13 5G 256GB"),
        ("Apple iPhone 15 (128 GB) - Black", "Apple iPhone 15 (256 GB) - Black"),
        ("Samsung T7 1TB SSD", "Samsung T7 2TB SSD"),
    ],
)
def test_storage_mismatch_is_variant(anchor, title):
    lab, reason = deterministic(anchor, None, cand(title))
    assert lab == "variant"
    assert reason.startswith("has ") and "listing is" in reason


def test_storage_only_on_one_side_is_not_a_conflict():
    assert label("Redmi 13 5G 128GB", "Redmi 13 5G (Orchid Pink)") == "same"


# --- prefilter / prompt / LLM labels --------------------------------------------------------


def test_prefilter_labels_every_candidate():
    cands = [
        cand("boAt Airdopes 141 Gen 2", cid=1),
        cand("Case for boAt Airdopes 141 Gen 2", cid=2),
        cand("boAt Airdopes 141", cid=3),
        cand("boAt Airdopes 121v2", cid=4),
        cand("boAt Airdopes 141 Gen 2", store="Snapmint", cid=5),
    ]
    resolve.prefilter(AIRDOPES, "boAt", cands)
    assert [c.label for c in cands] == ["same", "accessory", "variant", "different", "reseller"]
    assert all(c.reason for c in cands)


def test_resolve_prompt_lists_hints_and_prices():
    cands = [cand("boAt Airdopes 141 Gen 2", price=799, cid=7), cand("boAt Airdopes 141", price=None, cid=8)]
    resolve.prefilter(AIRDOPES, "boAt", cands)
    prompt = resolve.resolve_prompt(AIRDOPES, cands)
    assert prompt.startswith(f"ANCHOR: {AIRDOPES}")
    assert "7 | Croma | ₹799 | boAt Airdopes 141 Gen 2 | hint: same" in prompt
    assert "8 | Croma | ? | boAt Airdopes 141 | hint: variant" in prompt


def test_apply_llm_labels_updates_known_ids():
    cands = [cand("a", cid=1), cand("b", cid=2)]
    for c in cands:
        c.label, c.reason = "same", "rule"
    apply_llm_labels(cands, [
        {"id": 1, "label": "variant", "reason": "  Gen 1, not Gen 2  "},
        {"id": 2, "label": "same", "reason": ""},
    ])
    assert (cands[0].label, cands[0].reason) == ("variant", "Gen 1, not Gen 2")
    assert (cands[1].label, cands[1].reason) == ("same", "rule")  # empty reason keeps the old one


def test_apply_llm_labels_ignores_unknown_ids_and_labels():
    c = cand("a", cid=1)
    c.label, c.reason = "same", "rule"
    apply_llm_labels([c], [
        {"id": 99, "label": "different", "reason": "unknown id"},
        {"id": 1, "label": "accessory", "reason": "not an LLM label"},
        {"id": 1, "label": "maybe", "reason": "bogus"},
        {"id": "1", "label": "different", "reason": "string id"},
        {"label": "different"},
        {},
    ])
    assert (c.label, c.reason) == ("same", "rule")


def test_apply_llm_labels_truncates_reason():
    c = cand("a", cid=1)
    apply_llm_labels([c], [{"id": 1, "label": "different", "reason": "x" * 500}])
    assert c.label == "different"
    assert len(c.reason) == 120


def test_candidate_public_merges_extra():
    c = Candidate(id=3, title="t", store="s", price=1.0, link="l", origin="o", extra={"rating": 4.5})
    pub = c.public()
    assert pub["rating"] == 4.5
    assert pub["label"] == "pending"
    assert {"id", "title", "store", "price", "link", "origin", "reason"} <= pub.keys()


def test_resolve_schema_only_allows_llm_labels():
    enum = resolve.RESOLVE_SCHEMA["properties"]["labels"]["items"]["properties"]["label"]["enum"]
    assert enum == ["same", "variant", "different"]
