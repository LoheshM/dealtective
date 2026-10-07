"""Market reference, outliers and the verdict — computed in code, never by the LLM."""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

MIN_STORES = 3
GOOD_DEAL_AT = 0.93  # deal <= 93% of market reference
ABOVE_MARKET_AT = 1.07
LOW_OUTLIER = 0.6
HIGH_OUTLIER = 1.5
THEATRE_CLAIMED = 0.30  # listing claims at least this discount...
THEATRE_MRP_MULTIPLE = 1.5  # ...against an MRP at least this far above the market price

MAJOR_RETAILERS = {
    "amazon": "Amazon", "flipkart": "Flipkart", "croma": "Croma", "reliance digital": "Reliance Digital",
    "reliancedigital": "Reliance Digital", "vijay sales": "Vijay Sales", "vijaysales": "Vijay Sales",
    "tata cliq": "Tata CLiQ", "tatacliq": "Tata CLiQ", "myntra": "Myntra", "ajio": "AJIO", "nykaa": "Nykaa",
    "jiomart": "JioMart", "poorvika": "Poorvika", "sangeetha": "Sangeetha", "snapdeal": "Snapdeal",
    "meesho": "Meesho", "paytm": "Paytm Mall", "shopclues": "ShopClues", "lenskart": "Lenskart",
    "decathlon": "Decathlon", "firstcry": "FirstCry", "pepperfry": "Pepperfry", "ikea": "IKEA",
    "apple": "Apple", "samsung": "Samsung", "oneplus": "OnePlus", "mi.com": "Xiaomi", "dell": "Dell",
    "hp": "HP", "lenovo": "Lenovo", "unicorn": "Unicorn (Apple Premium Reseller)", "imagine": "Imagine",
    "girias": "Girias", "pai international": "Pai International", "bajaj electronics": "Bajaj Electronics",
}
QUICK_COMMERCE = ("zepto", "blinkit", "instamart", "swiggy", "bigbasket", "minutes", "bbnow", "flipkart minutes")
UNAVAILABLE = ("contact for", "out of stock", "unavailable", "not available")


@dataclass
class Offer:
    store: str
    price: float
    link: str | None
    title: str
    kind: str  # official | major | quick | other
    rating: float | None = None
    reviews: int | None = None
    notes: list[str] = field(default_factory=list)
    logo: str | None = None
    origin: str = ""
    available: bool = True
    flag: str | None = None  # low_outlier | high_outlier | None
    is_anchor: bool = False
    in_reference: bool = True  # False for the deal itself and its own marketplace

    def public(self) -> dict:
        return {
            "store": self.store, "price": self.price, "link": self.link, "title": self.title,
            "kind": self.kind, "rating": self.rating, "reviews": self.reviews, "notes": self.notes,
            "logo": self.logo, "origin": self.origin, "available": self.available, "flag": self.flag,
            "is_anchor": self.is_anchor, "in_reference": self.in_reference,
        }


def classify_store(store: str, brand: str | None) -> str:
    s = (store or "").lower()
    if brand:
        b = brand.lower().strip()
        # whole-word match: brand "Mi" must not make "Flipkart Minutes" official
        if b and (_word_in(b, s) or _word_in(re.sub(r"[^a-z0-9]", "", b), re.sub(r"[^a-z0-9 ]", "", s))):
            return "official"
    if any(_word_in(q, s) for q in QUICK_COMMERCE):
        return "quick"
    if any(_word_in(k, s) for k in MAJOR_RETAILERS):
        return "major"
    return "other"


def _word_in(needle: str, hay: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", hay) is not None


def store_key(store: str) -> str:
    """'Myntra - MNow' and 'Myntra' are one retailer; 'amazon.in' and 'Amazon.in' too."""
    base = re.split(r"\s+[-–|]\s+", (store or "").lower().strip())[0]
    base = re.sub(r"\.(com|in|co\.in|net|store)$", "", base)
    return re.sub(r"[^a-z0-9]", "", base)


def is_unavailable(notes: list) -> bool:
    joined = " ".join(str(n) for n in (notes or []) if n).lower()
    return any(u in joined for u in UNAVAILABLE)


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        raise ValueError("empty")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


@dataclass
class Market:
    status: str  # ok | thin
    store_count: int
    reference: float | None = None
    band_low: float | None = None
    band_high: float | None = None
    min_price: float | None = None
    max_price: float | None = None
    google_range: tuple[float, float] | None = None

    def public(self) -> dict:
        return {
            "status": self.status, "store_count": self.store_count, "reference": _r(self.reference),
            "band_low": _r(self.band_low), "band_high": _r(self.band_high), "min_price": _r(self.min_price),
            "max_price": _r(self.max_price),
            "google_range": list(self.google_range) if self.google_range else None,
        }


def compute_market(offers: list[Offer], google_range: tuple[float, float] | None = None) -> Market:
    """Reference = median of a trimmed set of independent, available store prices.

    The anchor (the deal being judged) is excluded so it cannot vouch for itself.
    Flags outliers on every offer, including the anchor.
    """
    pool = [o for o in offers if o.available and o.in_reference and not o.is_anchor and o.price > 0]
    # one price per store — keep the lowest listing a store shows
    by_store: dict[str, Offer] = {}
    for o in pool:
        key = store_key(o.store)
        if key not in by_store or o.price < by_store[key].price:
            by_store[key] = o
    prices = sorted(o.price for o in by_store.values())
    if not prices:
        return Market("thin", 0, google_range=google_range)
    raw_median = statistics.median(prices)
    trimmed = sorted(p for p in prices if 0.5 * raw_median <= p <= 2.0 * raw_median)
    if len(trimmed) < MIN_STORES:
        m = Market("thin", len(trimmed), google_range=google_range)
        if trimmed:
            m.min_price, m.max_price = trimmed[0], trimmed[-1]
        # Too thin for a verdict, but a price 2x away from everything else still deserves a flag.
        if len(prices) >= 2:
            base = statistics.median_low(prices)  # with 2 prices the mean-median sits between them
            for o in offers:
                if o.price < 0.5 * base:
                    o.flag = "low_outlier"
                elif o.price > 2.0 * base:
                    o.flag = "high_outlier"
        return m
    ref = statistics.median(trimmed)
    m = Market(
        "ok",
        len(trimmed),
        reference=ref,
        band_low=percentile(trimmed, 0.25),
        band_high=percentile(trimmed, 0.75),
        min_price=trimmed[0],
        max_price=trimmed[-1],
        google_range=google_range,
    )
    for o in offers:
        if o.price < LOW_OUTLIER * ref:
            o.flag = "low_outlier"
        elif o.price > HIGH_OUTLIER * ref:
            o.flag = "high_outlier"
    return m


@dataclass
class Verdict:
    label: str  # good_deal | fair_price | above_market | unusually_low | not_enough_data | no_deal_price
    deal_price: float | None
    mrp: float | None
    claimed_discount: float | None
    real_saving: float | None
    real_saving_abs: float | None
    mrp_multiple: float | None
    mrp_theatre: bool
    best_trusted: dict | None
    cheapest_any: dict | None
    headline: str

    def public(self) -> dict:
        return {
            "label": self.label, "deal_price": _r(self.deal_price), "mrp": _r(self.mrp),
            "claimed_discount": _p(self.claimed_discount), "real_saving": _p(self.real_saving),
            "real_saving_abs": _r(self.real_saving_abs), "mrp_multiple": _r(self.mrp_multiple, 1),
            "mrp_theatre": self.mrp_theatre, "best_trusted": self.best_trusted,
            "cheapest_any": self.cheapest_any, "headline": self.headline,
        }


def decide(market: Market, deal_price: float | None, mrp: float | None, offers: list[Offer]) -> Verdict:
    claimed = (mrp - deal_price) / mrp if mrp and deal_price and mrp > deal_price else None
    usable = [o for o in offers if o.available and o.flag != "low_outlier" and o.price > 0]
    # "Best trusted price" is an alternative to the listing being judged, never the listing itself.
    trusted = [o for o in usable if o.kind in {"official", "major", "quick"} and not o.is_anchor]
    best_trusted = min(trusted, key=lambda o: o.price).public() if trusted else None
    cheapest_any = min(usable, key=lambda o: o.price).public() if usable else None

    if market.status != "ok" or market.reference is None:
        head = "Not enough clean market data to judge this price"
        if claimed:
            head += f" — the listing claims {claimed:.0%} off"
        return Verdict("not_enough_data", deal_price, mrp, claimed, None, None, None, False,
                       best_trusted, cheapest_any, head)

    ref = market.reference
    mrp_multiple = mrp / ref if mrp else None
    if deal_price is None:
        return Verdict("no_deal_price", None, mrp, None, None, None, mrp_multiple, False, best_trusted,
                       cheapest_any, f"Market price today is about ₹{ref:,.0f} across {market.store_count} stores")

    real = (ref - deal_price) / ref
    real_abs = ref - deal_price
    theatre = bool(claimed and claimed >= THEATRE_CLAIMED and mrp_multiple and mrp_multiple >= THEATRE_MRP_MULTIPLE)
    if deal_price < LOW_OUTLIER * ref:
        label = "unusually_low"  # far below every store: worth verifying the seller before celebrating
    elif deal_price <= GOOD_DEAL_AT * ref:
        label = "good_deal"
    elif deal_price >= ABOVE_MARKET_AT * ref:
        label = "above_market"
    else:
        label = "fair_price"

    if real >= 0.005:
        saving = f"real saving vs market ₹{real_abs:,.0f} ({real:.0%})"
    elif real <= -0.005:
        saving = f"₹{-real_abs:,.0f} ({-real:.0%}) above the market price"
    else:
        saving = "same as the market price"
    if claimed:
        head = f"Claimed {claimed:.0%} off → {saving}"
    else:
        head = saving[0].upper() + saving[1:]
    return Verdict(label, deal_price, mrp, claimed, real, real_abs, mrp_multiple, theatre, best_trusted,
                   cheapest_any, head)


def _r(v: float | None, nd: int = 0) -> float | None:
    if v is None:
        return None
    return round(v, nd) if nd else round(v)


def _p(v: float | None) -> float | None:
    return None if v is None else round(v, 4)
