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
    "jiomart": "JioMart", "poorvika": "Poorvika", "sangeetha": "Sangeetha",
    "paytm": "Paytm Mall", "lenskart": "Lenskart", "shopsy": "Shopsy (Flipkart)",
    "decathlon": "Decathlon", "firstcry": "FirstCry", "pepperfry": "Pepperfry", "ikea": "IKEA",
    "apple": "Apple", "samsung": "Samsung", "oneplus": "OnePlus", "mi.com": "Xiaomi", "dell": "Dell",
    "hp": "HP", "lenovo": "Lenovo", "unicorn": "Unicorn (Apple Premium Reseller)", "imagine": "Imagine",
    "girias": "Girias", "pai international": "Pai International", "bajaj electronics": "Bajaj Electronics",
}
QUICK_COMMERCE = ("zepto", "blinkit", "instamart", "swiggy", "bigbasket", "minutes", "bbnow", "flipkart minutes")
UNAVAILABLE = ("contact for", "out of stock", "unavailable", "not available")
TRUSTED_KINDS = {"official", "major", "quick"}


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


# Cross-border resellers list Indian-rupee prices for imported stock. They are shown, never used
# for the Indian market price.
IMPORT_STORES = ("desertcart", "ubuy", "wafuu", "anjitait", "shoptheworld", "mygsm", "fado", "joom",
                 "aliexpress", "ebay", "zoodmall", "tradeling", "jomashop", "walmart", "bestbuy", "newegg")
_FOREIGN_TLD_RE = re.compile(r"\.(?:com\.)?(?:ae|sa|jp|uk|us|me|de|sg|my|cn|hk|au|ca|fr|it|es|nl|tr)$")


def classify_store(store: str, brand: str | None) -> str:
    s = (store or "").lower()
    if any(k in s for k in IMPORT_STORES) or _FOREIGN_TLD_RE.search(s.strip()):
        return "import"
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
    key = re.sub(r"[^a-z0-9]", "", base)
    # Retailers that list under several storefront names count once.
    for alias, canonical in STORE_ALIASES.items():
        if alias in key:
            return canonical
    return key


STORE_ALIASES = {"shopsy": "flipkart", "flipkartminutes": "flipkart", "myntramnow": "myntra"}


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
    basis: str = "all"  # "major" when ≥3 in-stock official/major/quick-commerce stores set the reference

    def public(self) -> dict:
        return {
            "status": self.status, "store_count": self.store_count, "reference": _r(self.reference),
            "basis": self.basis,
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
        # With 2–3 prices the median can be a junk listing, so anchor the check on better evidence:
        # Google's own price range, else the listing being judged, else the lower median.
        anchor = next((o.price for o in offers if o.is_anchor), None)
        base = (sum(google_range) / 2 if google_range else anchor) or (
            statistics.median_low(prices) if len(prices) >= 2 else None)
        if base:
            for o in offers:
                if o.price < 0.5 * base:
                    o.flag = "low_outlier"
                elif o.price > 2.0 * base:
                    o.flag = "high_outlier"
        return m
    # Prefer what established retailers charge: a long tail of small sellers listing at the old
    # price would otherwise inflate the "market" and make an ordinary sale price look like a steal.
    trusted = sorted(o.price for o in by_store.values()
                     if o.kind in TRUSTED_KINDS and 0.5 * raw_median <= o.price <= 2.0 * raw_median)
    basis_prices, basis = (trusted, "major") if len(trusted) >= MIN_STORES else (trimmed, "all")
    ref = statistics.median(basis_prices)
    m = Market(
        "ok",
        len(basis_prices),
        reference=ref,
        band_low=percentile(basis_prices, 0.25),
        band_high=percentile(basis_prices, 0.75),
        min_price=trimmed[0],
        max_price=trimmed[-1],
        google_range=google_range,
        basis=basis,
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
    same_price_at: list[str] = field(default_factory=list)  # trusted stores within 1% of the deal
    cheaper_at: dict | None = None  # cheapest trusted store more than 2% below the deal

    def public(self) -> dict:
        return {
            "label": self.label, "deal_price": _r(self.deal_price), "mrp": _r(self.mrp),
            "claimed_discount": _p(self.claimed_discount), "real_saving": _p(self.real_saving),
            "real_saving_abs": _r(self.real_saving_abs), "mrp_multiple": _r(self.mrp_multiple, 1),
            "mrp_theatre": self.mrp_theatre, "best_trusted": self.best_trusted,
            "cheapest_any": self.cheapest_any, "headline": self.headline,
            "same_price_at": self.same_price_at,
            "cheaper_at": ({"store": self.cheaper_at["store"], "price": _r(self.cheaper_at["price"]),
                            "link": self.cheaper_at.get("link")} if self.cheaper_at else None),
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
    # Context a shopper needs: is the same price available elsewhere, or is a trusted store cheaper?
    others = [o for o in trusted if o.in_reference]  # not the listing's own marketplace
    same_at = sorted({o.store for o in others if abs(o.price - deal_price) <= 0.01 * deal_price})[:3]
    cheaper = [o for o in others if o.price < 0.98 * deal_price]
    cheaper_at = min(cheaper, key=lambda o: o.price).public() if cheaper else None
    if cheaper_at and label == "good_deal":
        label = "fair_price"  # below the market median, but a trusted store is cheaper still
    return Verdict(label, deal_price, mrp, claimed, real, real_abs, mrp_multiple, theatre, best_trusted,
                   cheapest_any, head, same_at, cheaper_at)


def _r(v: float | None, nd: int = 0) -> float | None:
    if v is None:
        return None
    return round(v, nd) if nd else round(v)


def _p(v: float | None) -> float | None:
    return None if v is None else round(v, 4)
