# Dealtective — product & implementation plan

> **Dealtective, the deal detective.** Paste an Amazon.in link or a product name during the festive sale. In about 10 seconds Dealtective tells you:
> - what the market actually charges for the *exact same product* across Indian stores;
> - your real saving vs the claimed discount;
> - how far the MRP sits above any real price;
> - which listings are suspiciously cheap;
> - what independent users say.
>
> Every number comes from live SerpApi data and links to its source.

**Track:** Commerce & Market Intelligence · **Deadline:** 10 Oct 2026, 23:59 IST · How we got here: [IDEA_ITERATIONS.md](IDEA_ITERATIONS.md)

## 1. Problem

- During Big Billion Days and the Great Indian Festival, almost every listing advertises 50–80% off.
- Shoppers can't tell a real deal from MRP theatre:
  - *"In the name of sale they are selling items at much higher prices by giving fake discount on inflated MRP."* (DesiDime)
  - 56% of 54k LocalCircles respondents saw positively biased ratings.
- Existing trackers (Keepa, PriceHistory, BuyHatke) follow one product on one store. They don't tell you what the rest of the market charges for the same item today, and they don't check the MRP at all.

**Live example (8 Oct 2026):**
- Amazon.in sells boAt Airdopes 141 Gen 2 at **₹799**, "−80%" off an **M.R.P. of ₹3,990**, under a *Great Indian Festival* badge.
- Across Indian stores the market price for the same product is about ₹800.
- The real saving today is roughly zero. The MRP is about 5× anything anyone charges.

## 2. What it is (and isn't)

| Is | Isn't |
|---|---|
| A verification pipeline: Amazon listing → same product across Indian stores → computed market band | A price-history tracker (we don't claim to detect pre-sale hikes) |
| Neutral and numeric: "claimed 80% · real saving ₹40 (5%)" | An accuser: we never say "fake" or "counterfeit"; far-below-market listings get "verify seller" |
| LLM used where judgement is needed: same-exact-product matching and a plain-language summary | LLM arithmetic: all prices and percentages are computed in code |
| Honest about thin data: "Not enough market data" below 3 clean stores | A guesser |

## 3. User flow

1. Landing page: a search box with the hint "Paste an Amazon.in link or type a product". Three example chips (the demo products) and a live **credits left** pill.
2. Submit, and a **live trail** streams each step:
   - "Reading Amazon listing (Amazon Product API)…"
   - "Finding the same product on Google Shopping…"
   - "Matching variants: 6 of 10 cards are the same product (AI resolver)…"
   - "Pulling 13 stores (Google Immersive Product)…"
   - Each SerpApi call shows engine, purpose, time, and whether it was **live (1 credit)** or **cached (free)**.
3. **Verdict card:**
   - Headline label: *Good deal / Fair price / Above market / Not enough data*.
   - Claimed discount vs **real saving vs market**, MRP multiple, store count.
4. **Price number line (the key screen):**
   - Market band shaded; every store a dot with its logo.
   - The deal price highlighted; the MRP isolated on the right ("no store charges this").
   - Outliers greyed with "verify seller".
5. **Store table:** price, store type (Official / Major retailer / Quick commerce* / Marketplace / Other), rating and reviews, delivery note, link. Cheapest *trustworthy* option is marked.
6. **What users say:**
   - Rating distribution across stores.
   - An AI summary of themes, each bullet citing a forum thread, YouTube review or user review.
   - Amazon's own review summary for contrast.
7. **Excluded listings** (collapsible): "excluded: different variant (ANC)", "excluded: accessory", "excluded: gift/EMI reseller". This keeps the resolver transparent.
8. Optional **"Where else is this photo listed?"** runs Lens on the resolved product image (+1 credit).
9. **Share** copies a WhatsApp-ready one-line summary.

## 4. Architecture

```
browser (static SPA, vanilla JS + SVG)
   │  EventSource GET /api/check/stream?q=…
   ▼
FastAPI (app/main.py)
   └─ pipeline.run(query) ──emits events──► SSE
        ├─ parse.classify(query)        → amazon_asin | url_slug | text
        ├─ SerpClient.search(...)        → disk cache · fixture replay · credit budget (3/verdict)
        │     amazon_product | amazon  → anchor listing (price, MRP, rating, seller, image)
        │     google_shopping (gl=in)  → candidate product cards
        │     google_immersive_product → stores (more_stores), ratings, reviews, forums, videos
        │     google_lens (on demand)  → photo provenance
        ├─ resolve.match(anchor, cards)  → deterministic filter + 1 batched LLM call (JSON)
        ├─ market.compute(anchor, offers)→ trimmed median, band, outliers, saving, MRP multiple, label
        └─ llm.narrate(facts, voices)    → summary + cited user-voice bullets (no numbers invented)
```

**Credit discipline:**
- Hard cap of 3 credits per verdict, +1 for an explicit Lens request. `stores_next_page_token` is never fetched.
- SerpApi's 1-hour cache (identical params are free) plus our own disk cache (24 h TTL).
- The free Account API drives the credits pill.

**Replay mode:**
- `DEALTECTIVE_MODE=replay` serves recorded SerpApi and LLM responses from `data/fixtures/`.
- Judges can run the three demo products with **no keys at all**.
- Fixtures are scrubbed of `api_key`.

**LLM:**
- OpenAI, one provider, model configurable (`OPENAI_MODEL`, default `gpt-5.4-mini`).
- Structured JSON output; responses cached on disk by prompt hash.
- With no key, the resolver falls back to deterministic matching only and the narrative falls back to a template.

## 5. Core logic (all in code, all unit-tested)

**Price parsing:**
- Handles `"₹3,990"`, `"₹799.00"` and `"80% off₹3,990"`. That last one is the SerpApi `extracted_old_price: 80` trap.
- We always parse the `old_price` string ourselves.

**Entity resolution:**
1. Deterministic score: brand match, model-token overlap, and **variant conflicts**. A token like `ANC`, `Pro`, `Gen 2`, `Elite`, `Max`, `Plus`, `Lite`, storage (`128GB`) or RAM present on one side only means "different".
2. Accessory and reseller filters: case, cover, skin, tempered, strap, charger-only; gift, EMI, corporate.
3. One batched LLM call labels the survivors `same | variant | different` with a reason. With no LLM, the deterministic label is used.

**Market:**
- Clean offers = matched, not filtered, price > 0.
- Trimmed set = prices within 0.5×–2× the raw median.
- Reference = median of the trimmed set (the Amazon anchor itself is excluded).
- Band = 25th–75th percentile.
- Outliers: below 0.6× reference → "far below market — verify seller"; above 1.5× → "above market".

**Numbers shown:**
- **Real saving** = (reference − deal) / reference.
- **Claimed discount** = (MRP − deal) / MRP.
- **MRP multiple** = MRP / reference.

**Verdict label:**
- Fewer than 3 clean stores → *Not enough data*.
- Deal ≤ 0.93 × reference → *Good deal*.
- Deal ≥ 1.07 × reference → *Above market*.
- Otherwise → *Fair price*.
- Deal < 0.6 × reference → *Unusually low · verify seller*.
- An *MRP theatre* flag is raised when the claimed discount is ≥ 30% **and** the M.R.P. is ≥ 1.5× the market reference. (Revised during build: "real saving < 10%" missed cases where a 16% real saving sat beside an 80% claim.)

**Store classes:**
- *Official:* the store name contains the brand.
- *Major retailer:* curated Indian list (Amazon, Flipkart, Croma, Reliance Digital, Vijay Sales, Tata CLiQ, Myntra, AJIO, Nykaa, JioMart, Poorvika, Sangeetha, …).
- *Quick commerce:* Zepto, Blinkit, Instamart, bigbasket, Flipkart Minutes. These get a "may vary by pincode" note.
- *Other.*
- **Cheapest trustworthy** = cheapest offer from Official or Major retailer (Quick commerce is allowed too), not an outlier.

## 6. API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/check/stream?q=` | SSE stream: events `step`, `serp_call`, `anchor`, `match`, `market`, `voices`, `summary`, `done`, `error` |
| GET | `/api/lens/stream?image=&title=` | Optional Lens provenance (+1 credit) |
| GET | `/api/status` | mode (live/replay), credits left (free Account API), model |
| GET | `/api/examples` | demo queries available offline |
| GET | `/` | the SPA |

## 7. Repository layout

```
app/        config.py · serp.py · parse.py · resolve.py · market.py · llm.py · pipeline.py · main.py
web/        index.html · styles.css · app.js            (no build step)
data/fixtures/   recorded SerpApi + LLM responses for replay (committed, scrubbed)
tests/      test_parse.py · test_market.py · test_resolve.py · test_pipeline_replay.py · test_api.py · ui_smoke.py
docs/       IDEA_ITERATIONS.md · PLAN.md
scripts/    record_fixtures.py · secret_scan.py
```

## 8. Test plan

- **Unit:** price parsing (including the `80% off₹3,990` trap), variant-conflict detection, accessory filter, trimmed median, band, outliers, verdict thresholds, store classification.
- **Replay end-to-end:** the full pipeline over recorded fixtures for 3 products. Assert labels, store counts and that no network is used (respx blocks it).
- **API:** the SSE endpoint emits the events in order; `/api/status` works with no keys.
- **UI smoke (Playwright):** load, run an example, assert that the verdict, number line and store table render, with no console errors. Screenshots in light and dark mode at desktop and mobile widths.
- **Live smoke:** one fresh product end to end (≤3 credits).
- **Security:** a secret scan of the git tree before every push; `.env` is ignored; fixtures are scrubbed.
- **Reviews:** an independent code-review agent and a security review before submission.

## 9. Credit budget (245 left at plan time)

| Use | Credits |
|---|---|
| Record 3 demo fixtures | ~10 |
| Live dev and testing | ~60 |
| Final live smoke and demo recording warm-up | ~15 |
| Reserve (judges, retries) | rest |

## 10. Submission checklist

- Public repo `LoheshM/dealtective` with README: problem, demo GIF/screens, setup with `uv`, replay mode, SerpApi usage table, AI-tools disclosure, MIT licence.
- Demo video under 3 minutes (the user records it from a local, git-ignored DEMO_SCRIPT.md).
- Form fields: track = Commerce & Market Intelligence; prior project = no (new for this hackathon; design ideas informed by the author's prior work, no code reused); AI tools = Claude Code (Opus) for research, planning, coding and review, and OpenAI in the product.
