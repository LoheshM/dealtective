"""Input classification, price parsing and title normalisation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})(?:[/?#]|$)", re.IGNORECASE)
_BARE_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


@dataclass(frozen=True)
class Query:
    kind: str  # "amazon_asin" | "url_slug" | "text"
    value: str  # ASIN or search text
    raw: str


def classify(raw: str) -> Query:
    q = (raw or "").strip()
    if not q:
        raise ValueError("empty query")
    if _BARE_ASIN_RE.match(q.upper()):
        return Query("amazon_asin", q.upper(), raw)
    if _URL_RE.match(q) or q.lower().startswith(("www.", "amazon.in", "amzn.")):
        url = q if _URL_RE.match(q) else "https://" + q
        host = (urlparse(url).hostname or "").lower()
        if "amazon." in host:
            m = _ASIN_RE.search(urlparse(url).path + "/")
            if m:
                return Query("amazon_asin", m.group(1).upper(), raw)
        slug = slug_text(url)
        if slug:
            return Query("url_slug", slug, raw)
        raise ValueError("could not read a product from that link — try pasting the product name")
    return Query("text", re.sub(r"\s+", " ", q)[:150], raw)


_FOREIGN_CCY_RE = re.compile(r"[$€£¥]|(?<![a-z])(usd|aed|eur|gbp|sar|cad|aud)(?![a-z])", re.IGNORECASE)

# Store links Nijam can read through Google's index of the page (no dedicated SerpApi engine).
STORE_LABELS = {
    "flipkart.com": "Flipkart", "myntra.com": "Myntra", "ajio.com": "AJIO", "croma.com": "Croma",
    "reliancedigital.in": "Reliance Digital", "vijaysales.com": "Vijay Sales", "tatacliq.com": "Tata CLiQ",
    "nykaa.com": "Nykaa", "nykaafashion.com": "Nykaa Fashion", "jiomart.com": "JioMart", "meesho.com": "Meesho",
    "snapdeal.com": "Snapdeal", "boat-lifestyle.com": "boAt", "mi.com": "Mi.com", "samsung.com": "Samsung",
    "apple.com": "Apple", "decathlon.in": "Decathlon", "firstcry.com": "FirstCry", "pepperfry.com": "Pepperfry",
}


def store_of(url: str) -> tuple[str, str]:
    """('flipkart.com', 'Flipkart') for any Flipkart URL; falls back to the bare domain."""
    host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    host = re.sub(r"^(www|m|dl)\.", "", host)
    for domain, label in STORE_LABELS.items():
        if host == domain or host.endswith("." + domain):
            return domain, label
    return host, host


def listing_id(url: str) -> str | None:
    """Stable listing id in a store URL: Flipkart 'itm…', Myntra numeric id, etc."""
    path = unquote(urlparse(url if "://" in url else "https://" + url).path)
    m = re.search(r"(itm[0-9a-z]{8,})", path, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    ids = [p for p in path.split("/") if re.fullmatch(r"[0-9a-z]{6,}", p, re.IGNORECASE) and re.search(r"[0-9]", p)]
    return ids[-1].lower() if ids else None


# Words after which a URL slug stops naming the product: "asics noosa tri 16 | running shoes men".
CATEGORY_WORDS = {
    "running", "shoes", "shoe", "sneakers", "sandals", "slippers", "boots", "tshirt", "t", "shirt", "shirts",
    "jeans", "kurta", "kurti", "saree", "dress", "for", "men", "mens", "women", "womens", "boys", "girls",
    "kids", "unisex", "buy", "online", "with", "price",
}


def product_query(slug: str, max_words: int = 7) -> str:
    """'asics noosa tri 16 running shoes men' -> 'asics noosa tri 16' (keeps ≥2 words)."""
    words = slug.split()
    for i, w in enumerate(words):
        if i >= 2 and w.lower() in CATEGORY_WORDS:
            return " ".join(words[:i][:max_words])
    return " ".join(words[:max_words])


_SNIPPET_PRICE_RE = re.compile(r"(\d{1,2})%\.?\s*(?:off\s*)?₹?\s?([\d,]{3,})\.?\s*₹\s?([\d,]{2,})")


def snippet_prices(result: dict) -> tuple[float | None, float | None]:
    """(price, mrp) from a Google organic result for a store page.

    Uses the structured rich snippet when present, else Flipkart-style text: '51%. 11,999. ₹5,899.'
    """
    rich = ((result.get("rich_snippet") or {}).get("top") or {}).get("detected_extensions") or {}
    snippet = str(result.get("snippet") or "")
    m = _SNIPPET_PRICE_RE.search(snippet)
    mrp = parse_inr(m.group(2)) if m else None
    if rich.get("price") and rich.get("currency") in (None, "₹", "INR"):
        price = parse_inr(rich.get("price"))
    elif m:
        price = parse_inr(m.group(3))
    else:
        price = parse_inr(re.search(r"₹\s?[\d,]+(?:\.\d+)?", snippet).group(0)) if "₹" in snippet else None
    if mrp and price and mrp <= price:
        mrp = None
    return price, mrp


def slug_text(url: str) -> str:
    """Best-effort product name from a store URL path (Flipkart, Croma, etc.)."""
    path = unquote(urlparse(url).path)
    parts = [p for p in path.split("/") if p]
    best = ""
    for p in parts:
        if p.lower() in {"p", "dp", "product", "products", "buy", "itm"} or re.fullmatch(r"[a-z0-9]{6,}", p, re.IGNORECASE) and "-" not in p:
            continue
        words = re.sub(r"[-_+]+", " ", p).strip()
        if len(words) > len(best) and " " in words:
            best = words
    return re.sub(r"\s+", " ", best)[:150]


# --- prices -----------------------------------------------------------------

_NUM_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE)
_PLAIN_NUM_RE = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)")


def parse_inr(value: object) -> float | None:
    """Parse an Indian-rupee price.

    Handles "₹3,990", "₹799.00", "Rs. 1,299", "80% off₹3,990" (returns 3990, never 80)
    and plain numbers. Returns None when no price is present.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    s = str(value)
    if "₹" not in s and _FOREIGN_CCY_RE.search(s):
        return None  # "$140.00" is not ₹140
    m = _NUM_RE.search(s)
    if m:
        n = float(m.group(1).replace(",", ""))
        return n if n > 0 else None
    if "%" in s:
        return None  # a bare percentage is never a price
    m = _PLAIN_NUM_RE.search(s)
    if m:
        n = float(m.group(1).replace(",", ""))
        return n if n > 0 else None
    return None


# --- titles -------------------------------------------------------------------

# Words that distinguish one variant from another. Present on one side only => different product.
VARIANT_WORDS = {
    "anc", "enc", "pro", "max", "plus", "lite", "mini", "ultra", "elite", "neo", "prime", "air", "fe",
    "se", "edge", "fold", "flip", "nano", "slim", "turbo", "sport", "5g", "4g", "note",
}
_GEN_RE = re.compile(r"\b(?:gen(?:eration)?\s*([0-9]+)|([0-9]+)(?:st|nd|rd|th)\s*gen)\b", re.IGNORECASE)
_STORAGE_RE = re.compile(r"\b([0-9]{1,4})\s*(gb|tb)\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

ACCESSORY_WORDS = {
    "case", "cover", "skin", "pouch", "sleeve", "tempered", "protector", "strap", "band", "holder",
    "stand", "sticker", "decal", "replacement", "spare", "tips", "eartips", "cable", "adapter",
    "charger", "dock", "keychain", "lanyard", "mount", "film", "guard", "dummy", "cushion", "cushions",
    "earpads", "earpad", "pads", "foam", "screenguard",
}
# "Compatible with X" / "for OnePlus Nord" phrasing is how accessory listings name the product they fit.
ACCESSORY_PHRASE_RE = re.compile(r"(?<![a-z])(compatible (with|for)|replacement for)(?![a-z])|के लि", re.IGNORECASE)  # Hindi "के लिए" = "for"


def fix_mojibake(text: str) -> str:
    """Some marketplace titles arrive double-encoded ('à¤•à¥‡' instead of 'के'). Repair when possible."""
    if not text or "à¤" not in text and "à¥" not in text:
        return text
    out = bytearray()
    for ch in text:  # byte-by-byte: tolerate characters that were already lost upstream
        for enc in ("cp1252", "latin-1"):
            try:
                out += ch.encode(enc)
                break
            except UnicodeEncodeError:
                continue
        else:
            out += ch.encode("utf-8")
    fixed = out.decode("utf-8", errors="ignore")
    return fixed if fixed.strip() else text
RESELLER_WORDS = {"gift", "gifts", "gifting", "corporate", "emi", "snapmint", "bulk", "wholesale"}

COLOUR_WORDS = {
    "black", "white", "blue", "red", "green", "grey", "gray", "silver", "gold", "pink", "purple", "orange",
    "yellow", "teal", "orchid", "diamond", "midnight", "starlight", "titanium", "graphite", "mint", "lavender",
    "hawaiian", "ocean", "sky", "forest", "space", "rose", "cream", "beige", "navy", "active", "bold",
}

STOPWORDS = {
    "with", "and", "for", "the", "a", "an", "of", "in", "on", "to", "by", "up", "upto", "mic", "hrs",
    "hours", "playback", "bluetooth", "wireless", "true", "earbuds", "earphones", "tws", "buds",
    "black", "white", "blue", "red", "green", "grey", "gray", "silver", "gold", "active", "bold",
    "colour", "color", "new", "latest", "launch", "edition", "india", "online", "buy",
}


_SIZE_SPLIT_RE = re.compile(r"([0-9])\s+(gb|tb|mb)(?![a-z])", re.IGNORECASE)


def tokens(title: str) -> list[str]:
    # "128 GB" and "128GB" must be the same token
    return _TOKEN_RE.findall(_SIZE_SPLIT_RE.sub(r"\1\2", (title or "").lower()))


def title_head(title: str) -> str:
    """The part of a listing title before the first comma / bracket / pipe / ' - '."""
    return re.split(r"[,|(\[]| - | – ", title or "", maxsplit=1)[0]


# A genuine product mentions its own parts as features: "with Charging Case", "Padded Ear Cushions".
_FEATURE_PRECEDERS = {"charging", "with", "padded", "ear", "wireless", "carry", "carrying", "magnetic", "and"}


def accessory_word(title: str, anchor_title: str = "") -> str | None:
    """Return the accessory word if `title` is an accessory listing ('Silicone Case for Airdopes 141',
    '80mm Headphone Cushion Compatible with Rockerz 450'), else None.

    If the anchor itself is an accessory (the user is pricing a case), nothing is filtered.
    """
    if anchor_title and accessory_word(anchor_title):
        return None
    # "Stand-alone air fryer" is not a stand
    title = re.sub(r"stand[\s-]?alone", "standalone", title or "", flags=re.IGNORECASE)
    toks = tokens(title_head(title)) or tokens(title)
    for i, t in enumerate(toks):
        if t not in ACCESSORY_WORDS:
            continue
        # "Charging Cable for Airdopes 141": an accessory word followed by "for" is always an accessory
        if i == 0 or toks[i - 1] not in _FEATURE_PRECEDERS or "for" in toks[i + 1:i + 4]:
            return t
    if ACCESSORY_PHRASE_RE.search(title or ""):
        return "compatible-with listing"
    return None


def generation(title: str) -> str | None:
    m = _GEN_RE.search(title or "")
    if not m:
        return None
    return m.group(1) or m.group(2)


def storage(title: str) -> set[str]:
    return {f"{n}{u.lower()}" for n, u in _STORAGE_RE.findall(title or "")}


NOISE_CANCEL_RE = re.compile(r"noise[\s-]?cancel", re.IGNORECASE)


def variant_markers(title: str) -> set[str]:
    toks = set(tokens(title))
    marks = {t for t in toks if t in VARIANT_WORDS}
    g = generation(title)
    if g:
        marks.add(f"gen{g}")
    return marks


def core_tokens(title: str) -> set[str]:
    return {t for t in tokens(title) if t not in STOPWORDS and len(t) > 1 or t.isdigit()}


def short_query(title: str, brand: str | None = None, max_words: int = 7) -> str:
    """Compact search query from a long marketplace title.

    'boAt Airdopes 141 Gen 2, 4 Mics ENx Tech, 48 Hrs Playback, ...' -> 'boAt Airdopes 141 Gen 2'
    """
    head = re.split(r"[,|(\[]| - | – ", title or "", maxsplit=1)[0].strip()
    words = head.split()
    if brand and words and brand.lower() not in head.lower():
        words = [brand, *words]
    return " ".join(words[:max_words])


def clean_brand(raw: str | None) -> str | None:
    if not raw:
        return None
    b = re.sub(r"^(visit the|brand:)\s+", "", raw.strip(), flags=re.IGNORECASE)
    b = re.sub(r"\s+store$", "", b, flags=re.IGNORECASE)
    return b.strip() or None


def family_query(title: str, brand: str | None = None, max_words: int = 6) -> str:
    """Broader query without generation / variant words: 'boAt Airdopes 141 Gen 2' -> 'boAt Airdopes 141'."""
    head = short_query(title, brand, max_words=10)
    head = _GEN_RE.sub(" ", head)
    head = _STORAGE_RE.sub(" ", head)
    drop = VARIANT_WORDS | COLOUR_WORDS | {"ram", "rom", "storage", "gb", "tb", "with", "and"}
    words = [w for w in head.split() if w.lower().strip("+,") not in drop]
    return " ".join(words[:max_words]).strip() or head


def guess_brand(query: str, title: str | None = None) -> str | None:
    """First word of a typed query is usually the brand ('Redmi 13 5G' -> 'Redmi').

    Only trusted when it is alphabetic and, if a listing title is known, appears in it.
    """
    words = (query or "").split()
    if not words or not re.fullmatch(r"[A-Za-z][A-Za-z&.-]*", words[0]):
        return None
    w = words[0]
    if len(w) < 2 or w.lower() in STOPWORDS or w.lower() in VARIANT_WORDS:
        return None
    if title is not None and w.lower() not in (title or "").lower():
        return None
    return w
