"""The Dealtective verification pipeline.

query -> Amazon.in anchor listing -> Google Shopping candidates -> same-product resolution
      -> Google Immersive Product stores -> resolution -> market math -> narrative

Emits events through `emit(event, data)` so the UI can stream progress.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from . import market as mk
from . import parse, resolve
from .llm import LLM, NARRATE_SCHEMA, NARRATE_SYSTEM
from .serp import (
    BudgetExceeded,
    CreditBudget,
    ReplayMiss,
    SerpCall,
    SerpClient,
    SerpError,
)

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
log = logging.getLogger(__name__)

GSHOP = {"gl": "in", "hl": "en", "google_domain": "google.co.in"}
AMZ = {"amazon_domain": "amazon.in"}


@dataclass
class Deps:
    serp: SerpClient
    llm: LLM
    credits_per_check: int = 3


@dataclass
class Anchor:
    source: str  # "amazon" | "shopping"
    title: str
    brand: str | None
    price: float | None
    mrp: float | None
    link: str | None
    image: str | None
    rating: float | None = None
    reviews: int | None = None
    asin: str | None = None
    seller: str | None = None
    badges: list[str] | None = None
    bought: str | None = None
    bank_offers: list[dict] | None = None
    review_summary: str | None = None
    store: str = "Amazon.in"  # whose listing is being judged
    price_note: str | None = None  # caveat shown next to the price (e.g. read from Google's index)
    offer_price: float | None = None  # "Buy at ₹X" with bank/coupon offers, when the store shows it

    def public(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


class StepTracker:
    def __init__(self, emit: Emit):
        self.emit = emit

    async def __call__(self, step: str, status: str, text: str, **extra: Any) -> None:
        await self.emit("step", {"id": step, "status": status, "text": text, **extra})


async def run(raw_query: str, deps: Deps, emit: Emit) -> dict[str, Any]:
    step = StepTracker(emit)
    budget = CreditBudget(deps.credits_per_check)
    calls: list[dict] = []

    async def on_call(c: SerpCall) -> None:
        ev = c.to_event()
        calls.append(ev)
        await emit("serp_call", ev)

    async def serp(engine: str, params: dict, purpose: str) -> dict | None:
        try:
            d = await deps.serp.search(engine, params, purpose=purpose, budget=budget, on_call=on_call)
            return d if isinstance(d, dict) else None
        except BudgetExceeded as e:
            await emit("notice", {"text": f"Skipped {engine}: {e}"})
        except ReplayMiss:
            await emit("notice", {"text": "Offline demo mode: this query isn't recorded. Try an example, or add a SerpApi key."})
        except SerpError as e:
            await emit("notice", {"text": f"{engine}: {e}"})
        except Exception:
            log.exception("serp %s failed", engine)
            await emit("notice", {"text": f"{engine}: unexpected response — skipped"})
        return None

    llm_notified = False

    async def resolve_with_llm(title: str, cands: list[resolve.Candidate]) -> None:
        nonlocal llm_notified
        src = await llm_resolve(deps.llm, title, cands)
        if src in {"unavailable", "error"} and not llm_notified:
            llm_notified = True
            await emit("notice", {"text": "AI resolver unavailable — using stricter rules-only matching."})

    raw_query = await expand_short_link(raw_query)
    try:
        q = parse.classify(raw_query)
    except ValueError as e:
        await emit("error", {"text": str(e)})
        return {"ok": False}
    d2c_brand = None
    if q.kind == "url_slug":
        dom, lab = parse.store_of(raw_query)
        if dom in parse.D2C_STORES:
            d2c_brand = lab
            if lab.lower() not in q.value.lower():
                q = parse.Query("url_slug", f"{lab} {q.value}", q.raw)  # "boAt airdopes 141"
    await step("parse", "done", {
        "amazon_asin": f"Amazon.in product {q.value}",
        "url_slug": f"Store link → “{q.value}”",
        "text": f"Product search → “{q.value}”",
    }[q.kind], kind=q.kind)

    # 1 — the listing being judged -----------------------------------------------------------
    anchor: Anchor | None = None
    store_domain, store_label = parse.store_of(raw_query) if q.kind == "url_slug" else ("amazon.in", "Amazon.in")
    await step("anchor", "running", f"Reading the {store_label} listing")
    if q.kind == "amazon_asin":
        d = await serp("amazon_product", {"asin": q.value, **AMZ},
                       "Read the Amazon.in listing: price, M.R.P., seller, rating")
        if d:
            anchor = anchor_from_product(d, q.value)
    elif q.kind == "url_slug":
        # No SerpApi engine for Flipkart/Myntra/Croma…: read the page's price from Google's index of it.
        d = await serp("google", {"q": f"site:{store_domain} {parse.product_query(q.value, max_words=5)}", **GSHOP},
                       f"Read the {store_label} listing from Google's index (price & M.R.P.)")
        if d:
            anchor = anchor_from_store_results(d, raw_query, q.value, store_domain, store_label)
    else:
        d = await serp("amazon", {"k": q.value, **AMZ}, "Find the product on Amazon.in (claimed price & M.R.P.)")
        if d:
            anchor = anchor_from_search(d, q.value)
    if anchor:
        await emit("anchor", anchor.public())
        await step("anchor", "done", f"{anchor.store}: {parse.short_query(anchor.title, None, 9)}")
    else:
        await step("anchor", "warn", f"Couldn't read a {store_label} price — judging the market only")

    # 2 — candidate product cards on Google Shopping --------------------------------------
    base_title = anchor.title if anchor else q.value
    brand = anchor.brand if anchor else None
    if d2c_brand:
        brand = d2c_brand
    if not brand and q.kind != "amazon_asin":
        brand = parse.guess_brand(q.value, anchor.title if anchor else None)
    # URL input: derive the query from the listing. Typed input: search what the user asked for.
    if q.kind == "amazon_asin" and anchor:
        sq = parse.short_query(base_title, brand)
    elif q.kind == "url_slug":
        sq = parse.product_query(q.value)  # "asics noosa tri 16", not "…running shoes men"
        base_title = anchor.title if anchor else sq
    else:
        sq = q.value
    await step("shopping", "running", f"Finding “{sq}” across Indian stores")
    g = await serp("google_shopping", {"q": sq, **GSHOP}, "Find the same product across Indian stores")
    cards = cards_from_shopping(g or {})
    # Without an Amazon listing, the user's own words define the product. (Borrowing a Shopping
    # card's title here once turned "Nord CE4 Lite" into a pre-owned "Nord CE 2 Lite".)
    resolve.prefilter(base_title, brand, cards)
    await resolve_with_llm(base_title, cards)
    same_cards = [c for c in cards if c.label == "same"]

    # Google Shopping is sensitive to exact wording; if the precise query found no exact
    # match, widen to the product family once and let the resolver pick the variant.
    fq = parse.family_query(sq, brand)
    if anchor and not any(c.extra.get("page_token") for c in same_cards) and fq.lower() != sq.lower():
        await step("shopping", "running", f"No exact match yet — widening to “{fq}”")
        g2 = await serp("google_shopping", {"q": fq, **GSHOP}, "Widen to the product family, then pick the exact variant")
        seen = {(c.store, c.title, c.price) for c in cards}
        extra = [c for c in cards_from_shopping(g2 or {}, start_id=100) if (c.store, c.title, c.price) not in seen]
        resolve.prefilter(base_title, brand, extra)
        await resolve_with_llm(base_title, extra)
        cards += extra
        same_cards = [c for c in cards if c.label == "same"]
    await emit("match", {"stage": "cards", "anchor_title": base_title,
                         "candidates": [c.public() for c in cards]})
    await step("shopping", "done" if cards else "warn",
               f"{len(same_cards)} of {len(cards)} Shopping cards are the exact same product" if cards
               else "Google Shopping returned no products")

    # 3 — all stores for the best matching card -------------------------------------------
    chosen = choose_card(same_cards, brand)
    stores: list[resolve.Candidate] = []
    im_pr: dict = {}
    if chosen is not None:
        await step("stores", "running", f"Pulling every store for “{parse.short_query(chosen.title, None, 8)}”")
        im = await serp("google_immersive_product",
                        {"page_token": chosen.extra["page_token"], "more_stores": "true"},
                        "Pull all Indian stores, ratings, reviews, forum threads & videos for this product")
        im_pr = (im or {}).get("product_results") or {}
        stores = candidates_from_stores(im_pr, start_id=1000)
        resolve.prefilter(base_title, brand, stores)
        await resolve_with_llm(base_title, stores)
        await emit("match", {"stage": "stores", "anchor_title": base_title,
                             "candidates": [c.public() for c in stores]})
        n_same = sum(1 for c in stores if c.label == "same")
        await step("stores", "done", f"{n_same} of {len(stores)} store listings verified as the same product")
    else:
        await step("stores", "warn", "No exact-match product page to expand — using Shopping cards only")

    # 4 — market math (code, not LLM) -----------------------------------------------------
    offers = build_offers(anchor, same_cards, stores, brand)

    # Store links: if the page itself wasn't readable (or only a near-match listing was), the
    # store's own entry in Google Shopping is the better source for its price.
    if q.kind == "url_slug" and (anchor is None or "closest" in (anchor.price_note or "")):
        own = anchor_from_offers(offers, raw_query, store_label, store_domain)
        if own:
            own.image = own.image or (anchor.image if anchor else None)
            anchor = own
            await emit("anchor", anchor.public())
            await step("anchor", "done", f"{store_label} price found in its Google Shopping listing")
            offers = build_offers(anchor, same_cards, stores, brand)

    # Google Shopping India is thin for some categories (fashion, footwear). Brand stores and
    # retailers often show their price in ordinary Google results, so widen once if needed.
    usable = [o for o in offers if o.available and o.in_reference and not o.is_anchor]
    if len(usable) < mk.MIN_STORES:
        await step("web", "running", f"Few stores so far — checking Google results for “{sq}” prices")
        d = await serp("google", {"q": f"{sq} price", **GSHOP},
                       "Find more Indian store prices in Google results (rich snippets)")
        web = candidates_from_web(d or {}, start_id=2000)
        resolve.prefilter(base_title, brand, web)
        await resolve_with_llm(base_title, web)
        await emit("match", {"stage": "web", "anchor_title": base_title, "candidates": [c.public() for c in web]})
        offers = build_offers(anchor, same_cards, stores + web, brand)
        if anchor is None and q.kind == "url_slug":
            anchor = anchor_from_offers(offers, raw_query, store_label, store_domain)
            if anchor:
                await emit("anchor", anchor.public())
                offers = build_offers(anchor, same_cards, stores + web, brand)
        n_web = sum(1 for c in web if c.label == "same")
        await step("web", "done" if n_web else "warn", f"{n_web} more store price{'s' if n_web != 1 else ''} from Google results")
    google_range = parse_range(im_pr.get("price_range"))
    m = mk.compute_market(offers, google_range)
    v = mk.decide(m, anchor.price if anchor else None, anchor.mrp if anchor else None, offers)
    await emit("market", {
        "market": m.public(),
        "verdict": v.public(),
        "offers": [o.public() for o in sorted(offers, key=lambda o: o.price)],
        "product": product_card(anchor, chosen, im_pr),
    })

    # 5 — what independent users say ------------------------------------------------------
    sources = collect_sources(im_pr, anchor)
    await emit("voices", {"ratings": im_pr.get("ratings"), "rating": im_pr.get("rating"),
                          "reviews": im_pr.get("reviews"), "sources": sources})
    await step("summary", "running", "Writing a plain-language summary")
    summary = await narrate(deps.llm, m, v, anchor, sources)
    await emit("summary", summary)
    await step("summary", "done", "Done")

    credits = sum(1 for c in calls if c["credit"])
    result = {"ok": True, "credits_used": credits, "calls": len(calls), "verdict": v.label}
    await emit("done", result)
    return result


# --- anchor extraction ---------------------------------------------------------------------

def _d(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _l(x: Any) -> list:
    return x if isinstance(x, list) else []


def _s(x: Any) -> str | None:
    return x if isinstance(x, str) and x.strip() else None


def anchor_from_product(d: dict, asin: str) -> Anchor | None:
    pr = _d(d.get("product_results"))
    if not _s(pr.get("title")):
        return None
    single = _d(_d(d.get("purchase_options")).get("single_offer"))
    sold_by = _d(single.get("features")).get("sold_by")
    seller = _s(sold_by.get("text")) if isinstance(sold_by, dict) else _s(sold_by)
    price = parse.parse_inr(pr.get("price")) or parse.parse_inr(single.get("price"))
    summary = _s(_d(_d(d.get("reviews_information")).get("summary")).get("text"))
    brand = parse.clean_brand(_s(pr.get("brand"))) or _s(_d(d.get("product_details")).get("brand_name"))
    offers = [b for b in _l(pr.get("bank_offers")) if isinstance(b, dict)]
    return Anchor(
        source="amazon", title=pr["title"], brand=brand, price=price, mrp=parse.parse_inr(pr.get("old_price")),
        link=_s(pr.get("link_clean")) or _s(pr.get("link")) or f"https://www.amazon.in/dp/{asin}",
        image=_s(pr.get("thumbnail")), rating=_num(pr.get("rating")), reviews=_num(pr.get("reviews")), asin=asin,
        seller=seller, badges=[b for b in _l(pr.get("badges")) if isinstance(b, str)],
        bought=_s(pr.get("bought_last_month")),
        bank_offers=[{"title": _s(b.get("title")), "content": _s(b.get("content"))} for b in offers][:4],
        review_summary=summary,
    )


def _num(x: Any) -> float | None:
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _overlap(query: str, title: str) -> float:
    qt = parse.core_tokens(query)
    tt = set(parse.tokens(title))
    # Amazon often drops the brand from titles ("iPhone 15 (128 GB) - Blue"); don't penalise that.
    brand = parse.guess_brand(query)
    if brand and brand.lower() not in tt:
        qt.discard(brand.lower())
    if not qt:
        return 0.0
    return len(qt & tt) / len(qt)


_MULTIPACK_RE = re.compile(r"\b(combo|bundle|twin ?pack|pack of \d+|set of \d+|\d+\s?(?:pcs|pieces|units)|"
                           r"(?:2|3|4)\s?x\s|buy \d+ get)\b", re.IGNORECASE)


def anchor_from_search(d: dict, query: str) -> Anchor | None:
    scored: list[tuple[float, float, dict]] = []
    q_models = resolve._model_numbers(query)
    q_marks = parse.variant_markers(query)
    for i, o in enumerate(_l(d.get("organic_results"))):
        if not isinstance(o, dict):
            continue
        title = _s(o.get("title")) or ""
        if not title or parse.parse_inr(o.get("price")) is None:
            continue
        if q_models and not all(resolve.has_model(title, m) for m in q_models):
            continue  # every model token the user typed must be in the listing (HD9252/90 ≠ HD9252/70)
        if parse.accessory_word(title, query):
            continue  # "80mm Headphone Cushion Compatible with Rockerz 450"
        if parse.variant_markers(resolve._head(title)) - q_marks:
            continue  # "Redmi Note 13" or "141 Pro" when the user asked for "Redmi 13" / "141"
        if _MULTIPACK_RE.search(title) and not _MULTIPACK_RE.search(query):
            continue  # "Combo Ninja Call Pro Plus … (Black) Ninja Call Pro Plus … (Grey)": two units, not the product
        score = _overlap(query, title) - (0.15 if o.get("sponsored") else 0) - i * 0.01
        scored.append((score, parse.parse_inr(o.get("price")), o))
    if not scored or max(s for s, _, _ in scored) < 0.5:
        return None
    # Several near-identical matches (different sellers/colours): judge the typical Amazon listing,
    # not whichever third-party seller happens to rank first. Take the cheapest close match that
    # isn't suspiciously far below the others.
    top = max(s for s, _, _ in scored)
    close = [(p, o) for s, p, o in scored if s >= top - 0.1]
    mid = statistics.median(p for p, _ in close)
    best = min((x for x in close if x[0] >= 0.6 * mid), key=lambda x: x[0])[1]
    return Anchor(
        source="amazon", title=best["title"], brand=None, price=parse.parse_inr(best.get("price")),
        mrp=parse.parse_inr(best.get("old_price")), link=_s(best.get("link_clean")) or _s(best.get("link")),
        image=_s(best.get("thumbnail")), rating=_num(best.get("rating")), reviews=_num(best.get("reviews")),
        asin=_s(best.get("asin")), badges=[b for b in _l(best.get("badges")) if isinstance(b, str)],
        bought=_s(best.get("bought_last_month")),
    )


# --- candidates ----------------------------------------------------------------------------

def cards_from_shopping(g: dict, start_id: int = 0) -> list[resolve.Candidate]:
    out = []
    seen: set[tuple] = set()
    for i, r in enumerate(_l(g.get("shopping_results"))):
        if not isinstance(r, dict):
            continue
        price = parse.parse_inr(r.get("price"))
        title = parse.fix_mojibake(_s(r.get("title")) or "") or None
        if not title or price is None:
            continue
        dedupe = (r.get("source"), title, price)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        out.append(resolve.Candidate(
            id=start_id + i, title=title, store=_s(r.get("source")) or "?", price=price,
            link=_s(r.get("product_link")), origin="google_shopping",
            extra={"page_token": _s(r.get("immersive_product_page_token")), "thumbnail": _s(r.get("thumbnail")),
                   "rating": _num(r.get("rating")), "reviews": _num(r.get("reviews")),
                   "old_price": parse.parse_inr(r.get("old_price")), "logo": _s(r.get("source_icon"))},
        ))
    return out


_NON_PRODUCT_PATH_RE = re.compile(r"/(c|sale|sales|blog|blog-listing|collections?|search|brand|brands|offers?|deals?|category|"
                                  r"lookalike|compare|reviews?)/", re.IGNORECASE)


def _url_key(u: str) -> str:
    """Comparable path for a store URL: lowercase, no query, no language prefix (/hi/), no trailing slash."""
    p = urlparse(u if "://" in u else "https://" + u)
    path = re.sub(r"^/(hi|ta|te|kn|ml|mr|bn|gu)/", "/", p.path.lower()).rstrip("/-")
    return re.sub(r"^(www|m|dl)\.", "", (p.hostname or "").lower()) + path


def _title_match(slug: str, title: str) -> float:
    """Overlap in both directions: short store titles ('Sony WH-1000XM5') vs long URL slugs."""
    a, b = parse.core_tokens(slug), parse.core_tokens(title)
    if not a or not b:
        return 0.0
    return max(len(a & b) / len(a), len(a & b) / len(b))


def _missing_codes(slug: str, title: str) -> set[str]:
    """Short codes / numbers from the product name that a candidate title leaves out ('NN', '2', 'XM5').

    Only the product-name part of the slug counts (before category words like 'running shoes').
    """
    name = set(parse.tokens(parse.product_query(slug)))
    have = set(parse.tokens(title))
    return {t for t in name - have if len(t) <= 3 or any(c.isdigit() for c in t)} - parse.COLOUR_WORDS


def anchor_from_store_results(d: dict, url: str, slug: str, domain: str, label: str) -> Anchor | None:
    """The listing a user pasted (Flipkart, Myntra…), read from Google's index of that store.

    Google sometimes ignores `site:` and returns other websites, so results are filtered to the
    store's own product pages. Preference: same URL path, then same listing id, then the closest
    product page on that store.
    """
    lid = parse.listing_id(url)
    want = _url_key(url)
    results = [r for r in _l(d.get("organic_results"))
               if isinstance(r, dict) and _s(r.get("link")) and parse.store_of(r["link"])[0] == domain]
    exact = [r for r in results if _url_key(r["link"]) == want or (lid and lid in r["link"].lower())]
    similar = [r for r in results if r not in exact and not _NON_PRODUCT_PATH_RE.search(urlparse(r["link"]).path)
               and _title_match(slug, r.get("title") or "") >= 0.6
               and not (parse.variant_markers(r.get("title") or "") - parse.variant_markers(slug))
               and not _missing_codes(slug, r.get("title") or "")]
    for r in exact + similar:
        price, mrp = parse.snippet_prices(r)
        rich = _d(_d(r.get("rich_snippet")).get("top")).get("detected_extensions") or {}
        is_exact = r in exact
        # A sibling listing's description text ("Buy … for Rs.34990 Online") is often the stale M.R.P.;
        # only trust a near match when Google gives a structured price for it.
        if not price or (not is_exact and not (rich.get("price") or rich.get("price_from"))):
            continue
        snippet = str(r.get("snippet") or "")
        offer = re.search(r"Buy at ₹\s?([\d,]+)", snippet)
        return Anchor(
            source=domain.split(".")[0], store=label, title=_s(r.get("title")) or slug, brand=None,
            price=price, mrp=mrp, link=url, image=_s(r.get("thumbnail")) or listing_image(d, lid, domain),
            rating=_num(rich.get("rating")), reviews=_num(rich.get("reviews")),
            offer_price=parse.parse_inr(offer.group(1)) if offer else None,
            price_note=("Price from Google's index of this page — the live price may differ"
                        + ("" if is_exact else " · closest matching listing on this store")),
        )
    return None


def anchor_from_offers(offers: list[mk.Offer], url: str, store_label: str, domain: str) -> Anchor | None:
    """Fallback for store links: the store's own price as it appears in Google Shopping / product data."""
    key = mk.store_key(store_label)
    mine = [o for o in offers if not o.is_anchor and (key and key in mk.store_key(o.store)
                                                       or domain.split(".")[0] in (o.link or "").lower())]
    mine = [o for o in mine if o.available] or mine
    if not mine:
        return None
    o = min(mine, key=lambda x: x.price)
    return Anchor(source=domain.split(".")[0], store=store_label, title=o.title, brand=None, price=o.price,
                  mrp=None, link=url, image=None, rating=o.rating, reviews=o.reviews,
                  price_note=f"Price from {store_label}'s Google Shopping listing — the live price may differ")


def listing_image(d: dict, lid: str | None, domain: str) -> str | None:
    """Product photo for a store listing from Google's `inline_images` block.

    Organic results for store pages often carry no thumbnail; the image strip usually has the
    store's own photo. Prefer the exact listing id, then any image from the same store, then any.
    """
    images = [im for im in _l(d.get("inline_images")) if isinstance(im, dict)]

    def url(im: dict) -> str | None:
        for k in ("original", "thumbnail"):
            u = _s(im.get(k))
            if u and u.startswith("https://"):
                return u
        return None

    for want in (lambda im: lid and lid in str(im.get("source") or "").lower(),
                 lambda im: domain in str(im.get("source") or "").lower(),
                 lambda im: True):
        for im in images:
            if want(im) and url(im):
                return url(im)
    for r in _l(d.get("organic_results")):  # a sibling listing's thumbnail (same product, other size)
        if isinstance(r, dict) and _s(r.get("thumbnail")) and domain in str(r.get("link") or ""):
            return r["thumbnail"]
    return None


def candidates_from_web(d: dict, start_id: int) -> list[resolve.Candidate]:
    """Store prices from ordinary Google results (rich snippets), rupee prices only."""
    out = []
    for i, r in enumerate(_l(d.get("organic_results"))):
        if not isinstance(r, dict) or not _s(r.get("link")):
            continue
        price, _mrp = parse.snippet_prices(r)
        if not price:
            continue
        _domain, label = parse.store_of(r["link"])
        out.append(resolve.Candidate(
            id=start_id + i, title=parse.fix_mojibake(_s(r.get("title")) or ""), store=_s(r.get("source")) or label,
            price=price, link=r["link"], origin="google",
            extra={"notes": ["Price from Google search snippet"], "logo": _s(r.get("favicon"))},
        ))
    return out


def candidates_from_stores(pr: dict, start_id: int) -> list[resolve.Candidate]:
    out = []
    for i, s in enumerate(_l(pr.get("stores"))):
        if not isinstance(s, dict):
            continue
        price = parse.parse_inr(s.get("price")) or parse.parse_inr(s.get("extracted_price"))
        if price is None:
            continue
        notes = [n for n in _l(s.get("details_and_offers")) if isinstance(n, str)]
        out.append(resolve.Candidate(
            id=start_id + i, title=parse.fix_mojibake(_s(s.get("title")) or _s(pr.get("title")) or ""),
            store=_s(s.get("name")) or "?",
            price=price, link=_s(s.get("link")), origin="google_immersive_product",
            extra={"rating": _num(s.get("rating")), "reviews": _num(s.get("reviews")), "logo": _s(s.get("logo")),
                   "notes": notes},
        ))
    return out


def choose_card(same: list[resolve.Candidate], brand: str | None) -> resolve.Candidate | None:
    with_token = [c for c in same if c.extra.get("page_token")]
    if not with_token:
        return None

    def rank(c: resolve.Candidate) -> tuple:
        kind = mk.classify_store(c.store, brand)
        pref = {"official": 0, "major": 1, "quick": 2, "other": 3, "import": 4}.get(kind, 5)
        return (pref, -(c.extra.get("reviews") or 0), c.id)

    return min(with_token, key=rank)


async def llm_resolve(llm: LLM, anchor_title: str, cands: list[resolve.Candidate]) -> str:
    """Refine rule labels with one batched LLM call. Returns the LLM source (cache/live/unavailable/error/skipped)."""
    pending = [c for c in cands if c.label in {"same", "variant"}]
    if not pending:
        return "skipped"
    data, src = await llm.json(resolve.RESOLVE_SYSTEM, resolve.resolve_prompt(anchor_title, pending),
                               resolve.RESOLVE_SCHEMA, "resolve")
    if isinstance(data, dict):
        resolve.apply_llm_labels(pending, [x for x in _l(data.get("labels")) if isinstance(x, dict)])
    return src


def build_offers(anchor: Anchor | None, cards: list[resolve.Candidate], stores: list[resolve.Candidate],
                 brand: str | None) -> list[mk.Offer]:
    # One row per retailer: prefer an in-stock listing, then the lowest price, then richer store rows.
    best: dict[str, mk.Offer] = {}
    for c in stores + cards:
        if c.label != "same" or not c.price:
            continue
        key = mk.store_key(c.store)
        notes = [n for n in _l(c.extra.get("notes")) if isinstance(n, str)]
        kind = mk.classify_store(c.store, brand)
        if kind == "quick":
            notes.append("Price may vary by pincode")
        if kind == "import":
            notes.append("International reseller · import price")
        o = mk.Offer(store=c.store, price=c.price, link=c.link, title=c.title, kind=kind,
                     rating=c.extra.get("rating"), reviews=c.extra.get("reviews"), notes=notes,
                     logo=c.extra.get("logo"), origin=c.origin, available=not mk.is_unavailable(notes))
        if kind == "import":
            o.in_reference = False
        # The listing being judged must not vouch for itself.
        if anchor and mk.store_key(anchor.store) in key:
            o.in_reference = False
        cur = best.get(key)
        if cur is None or (not cur.available, cur.price) > (not o.available, o.price):
            best[key] = o
    offers = list(best.values())
    if anchor and anchor.price:
        notes = [f"Sold by {anchor.seller}"] if anchor.seller else []
        if anchor.offer_price:
            notes.append(f"₹{anchor.offer_price:,.0f} with offers")
        if anchor.price_note:
            notes.append(anchor.price_note)
        offers.append(mk.Offer(store=f"{anchor.store} (this listing)", price=anchor.price, link=anchor.link,
                               title=anchor.title, kind="major", rating=anchor.rating, reviews=anchor.reviews,
                               notes=notes, origin=anchor.source, is_anchor=True))
    return offers


_SHORT_HOSTS = {"amzn.in", "amzn.to", "a.co", "amzn.eu"}


async def expand_short_link(raw: str) -> str:
    """Amazon app share links (amzn.in/d/…) redirect to the full product URL. Free, no SerpApi credit."""
    q = (raw or "").strip()
    m = re.match(r"^(?:https?://)?([^/\s]+)(/\S*)?$", q, re.IGNORECASE)
    if not m or m.group(1).lower() not in _SHORT_HOSTS:
        return raw
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=8.0, max_redirects=5) as c:
            r = await c.head("https://" + m.group(1).lower() + (m.group(2) or "/"))
            final = str(r.url)
        host = (httpx.URL(final).host or "").lower()
        return final if host.endswith(("amazon.in", "amazon.com")) else raw
    except Exception:  # noqa: BLE001 - expansion is best-effort; classify() reports unreadable links
        return raw


def parse_range(s: Any) -> tuple[float, float] | None:
    if not s:
        return None
    nums = [parse.parse_inr(p) for p in re.split(r"\s*[-–]\s*", str(s))]
    nums = [n for n in nums if n]
    if len(nums) == 2:
        return (min(nums), max(nums))
    return None


def product_card(anchor: Anchor | None, chosen: resolve.Candidate | None, pr: dict) -> dict:
    thumbs = pr.get("thumbnails") or []
    return {
        "title": (anchor.title if anchor else None) or pr.get("title") or (chosen.title if chosen else None),
        "image": (anchor.image if anchor else None) or (thumbs[0] if thumbs else None)
        or (chosen.extra.get("thumbnail") if chosen else None),
        "brand": (anchor.brand if anchor else None) or pr.get("brand"),
        "google_title": pr.get("title"),
    }


# --- voices & narrative ---------------------------------------------------------------------

def collect_sources(pr: dict, anchor: Anchor | None) -> list[dict]:
    src: list[dict] = []
    for r in [x for x in _l(pr.get("user_reviews")) if isinstance(x, dict)][:6]:
        text = (_s(r.get("text")) or "").strip()
        if text:
            src.append({"type": "review", "title": _s(r.get("title")) or "", "text": text[:500],
                        "rating": _num(r.get("rating")), "source": _s(r.get("source")), "link": _s(r.get("link"))})
    for f in [x for x in _l(pr.get("discussions_and_forums")) if isinstance(x, dict)][:5]:
        src.append({"type": "forum", "title": _s(f.get("title")) or "", "source": _s(f.get("source")),
                    "link": _s(f.get("link")),
                    "meta": " · ".join(str(x) for x in [f.get("date"), f.get("comments")] if x)})
    for v in [x for x in _l(pr.get("videos")) if isinstance(x, dict)][:4]:
        src.append({"type": "video", "title": _s(v.get("title")) or "",
                    "source": _s(v.get("channel")) or _s(v.get("source")), "link": _s(v.get("link")),
                    "thumbnail": _s(v.get("thumbnail")), "meta": _s(v.get("duration"))})
    if anchor and anchor.review_summary:
        src.append({"type": "store_summary", "title": "Amazon.in review summary",
                    "text": anchor.review_summary[:700], "source": "Amazon.in", "link": anchor.link})
    for i, s in enumerate(src):
        s["id"] = i
    return src


_RUPEE_RE = re.compile(r"₹\s?([0-9][0-9,]*)")
_PCT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s?%")
_MULT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s?[x×]", re.IGNORECASE)


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(text: object) -> str:
    """Model text without control characters (Gemini once emitted a backspace before each ₹)."""
    return _CONTROL_RE.sub("", str(text or "")).strip()


def numbers_grounded(text: str, facts: dict) -> bool:
    """True when every ₹ amount, percentage and multiple in `text` also appears in the computed facts."""
    blob = " ".join(str(v) for v in facts.values() if v is not None)
    allowed_rupees = {m.replace(",", "") for m in _RUPEE_RE.findall(blob)}
    allowed_pcts = {m for m in _PCT_RE.findall(blob)} | {str(v) for v in facts.values() if isinstance(v, int)}
    allowed_mult = {m for m in _MULT_RE.findall(blob)} | {f"{facts.get('mrp_multiple')}"}
    if any(m.replace(",", "") not in allowed_rupees for m in _RUPEE_RE.findall(text)):
        return False
    if any(m not in allowed_pcts for m in _PCT_RE.findall(text)):
        return False
    return all(m in allowed_mult or m.rstrip("0").rstrip(".") in allowed_mult for m in _MULT_RE.findall(text))


async def narrate(llm: LLM, m: mk.Market, v: mk.Verdict, anchor: Anchor | None, sources: list[dict]) -> dict:
    def inr(x: float | None) -> str | None:
        return f"₹{x:,.0f}" if x is not None else None

    facts = {
        "product": anchor.title[:140] if anchor else None,
        "label": v.label, "headline": v.headline,
        "deal_price": inr(v.deal_price), "mrp": inr(v.mrp),
        "claimed_discount_pct": round(v.claimed_discount * 100) if v.claimed_discount else None,
        "real_saving_vs_market": (
            f"{inr(v.real_saving_abs)} ({v.real_saving:.0%}) below the market reference" if v.real_saving and v.real_saving > 0.005
            else f"{inr(-v.real_saving_abs)} ({-v.real_saving:.0%}) ABOVE the market reference" if v.real_saving and v.real_saving < -0.005
            else "no saving vs the market reference" if v.real_saving is not None else None
        ),
        "real_saving_amount": inr(abs(v.real_saving_abs)) if v.real_saving_abs is not None else None,
        "real_saving_pct": round(abs(v.real_saving) * 100) if v.real_saving is not None else None,
        "market_reference": inr(m.reference), "stores": m.store_count,
        "market_min": inr(m.min_price), "market_max": inr(m.max_price),
        "mrp_multiple": v.public()["mrp_multiple"], "mrp_theatre": v.mrp_theatre,
        "best_trusted_store": v.best_trusted["store"] if v.best_trusted else None,
        "best_trusted_price": inr(v.best_trusted["price"]) if v.best_trusted else None,
    }
    src_txt = "\n".join(
        f"[{s['id']}] {s['type']} · {s.get('source') or ''} · {s.get('title', '')[:120]}"
        + (f" · rating {s['rating']}" if s.get("rating") else "")
        + (f"\n    {s['text'][:400]}" if s.get("text") else "")
        for s in sources
    )
    user = f"FACTS:\n{facts}\n\nSOURCES:\n{src_txt or '(none)'}"
    data, _origin = await llm.json(NARRATE_SYSTEM, user, NARRATE_SCHEMA, "narrate")
    if isinstance(data, dict):
        valid = {s["id"] for s in sources}
        voices = [
            {"point": _clean(vb.get("point", ""))[:240], "tone": vb.get("tone") if vb.get("tone") in {"positive", "negative", "mixed"} else "mixed",
             "source_ids": [i for i in _l(vb.get("source_ids")) if i in valid]}
            for vb in _l(data.get("voices")) if isinstance(vb, dict)
        ]
        summary = re.sub(r'"(₹[0-9,]+)"', r"\1", _clean(data.get("summary") or ""))  # some models quote amounts
        # The summary may only restate computed numbers; anything else falls back to the template.
        if summary and numbers_grounded(summary, facts):
            return {"summary": summary, "voices": [x for x in voices if x["source_ids"]], "generated_by": "llm"}
        return {"summary": template_summary(m, v), "voices": [x for x in voices if x["source_ids"]],
                "generated_by": "template"}
    return {"summary": template_summary(m, v), "voices": [], "generated_by": "template"}


def template_summary(m: mk.Market, v: mk.Verdict) -> str:
    if v.label == "not_enough_data" or m.reference is None:
        return "We couldn't find at least three independent Indian stores selling this exact product, so the price can't be judged fairly."
    s = f"{v.headline}. The market price across {m.store_count} Indian stores is about ₹{m.reference:,.0f}."
    if v.mrp_theatre and v.mrp_multiple:
        s += f" The M.R.P. is {v.mrp_multiple:.1f}× what stores actually charge."
    return s


# --- Lens (optional, +1 credit) -----------------------------------------------------------------

async def run_lens(image: str, title: str, deps: Deps, emit: Emit) -> dict:
    calls: list[dict] = []

    async def on_call(c: SerpCall) -> None:
        calls.append(c.to_event())
        await emit("serp_call", c.to_event())

    budget = CreditBudget(1)
    try:
        d = await deps.serp.search("google_lens", {"url": image, "country": "in", "hl": "en"},
                                   purpose="Where else is this exact product photo listed?", budget=budget,
                                   on_call=on_call)
    except (BudgetExceeded, ReplayMiss, SerpError) as e:
        await emit("error", {"text": f"Lens unavailable: {e}"})
        return {"ok": False}
    matches = []
    for i, v in enumerate([x for x in _l(_d(d).get("visual_matches")) if isinstance(x, dict)][:20]):
        pr = _d(v.get("price"))
        # Only rupee prices are comparable; Lens also returns $, AED etc. from foreign stores.
        rupee = pr.get("currency") == "₹" or "₹" in str(pr.get("value") or "")
        c = resolve.Candidate(id=i, title=_s(v.get("title")) or "", store=_s(v.get("source")) or "?",
                              price=parse.parse_inr(pr.get("extracted_value")) if rupee else None,
                              link=_s(v.get("link")), origin="google_lens",
                              extra={"thumbnail": _s(v.get("thumbnail")), "logo": _s(v.get("source_icon"))})
        c.label, c.reason = resolve.deterministic(title, None, c)
        matches.append(c.public() | {"kind": mk.classify_store(c.store, None)})
    await emit("lens", {"matches": matches})
    await emit("done", {"ok": True, "credits_used": sum(1 for c in calls if c["credit"])})
    return {"ok": True}
