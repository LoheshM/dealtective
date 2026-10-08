# Idea iterations — from pain point to product

How the idea was found, challenged and narrowed. Each round states the idea, the strongest objection, and what changed.

## Evidence base

**Research sources:**
- Reddit was unreachable from our research environment (network-blocked), so evidence comes from Indian deal and tech forums (DesiDime, TechEnclave, Technofino), large consumer surveys (LocalCircles, Indeed) and government/police advisories.
- Ranked pains (frequency × intensity × solvable with live search data):
  1. **Fake festive discounts**: prices or MRP raised before Big Billion Days / Great Indian Festival.
     - *"In the name of sale they are selling items at much higher prices by giving fake discount on inflated MRP."* ([DesiDime](https://www.desidime.com/discussions/the-big-billion-bluff))
     - *"Bezass raises prices by 15% at first and then pretends to offer a 5% discount."* (same thread)
     - PriceHistory's own audit: 19% of 325 sale products were marked up pre-sale, yet it says it *"neither confirms nor rules out MRP inflation"* because it only tracks selling price.
  2. **Counterfeits from marketplace sellers.**
     - *"I got fooled for fake … I'm really getting angry on myself"* ([TechEnclave](https://techenclave.com/t/got-fake-airpods-pro-from-amazon/259641?page=3))
     - About 1 in 5 online shoppers received fakes (LocalCircles).
  3. **Fake or biased reviews.** 56% of 54k LocalCircles respondents saw positively biased ratings; Fakespot shut down in 2025.
  4. Scam customer-care numbers, booking sites and job offers (high stakes, but noisy data).
  5. Small-seller competitor price monitoring (served by enterprise vendors only).

**Live data probe** (4 SerpApi credits, 8 Oct 2026, query `boAt Airdopes 141`):
- **Amazon.in** (`engine=amazon`) lists Airdopes 141 Gen 2 at **₹799 with "M.R.P. ₹3,990"**, i.e. 80% off, with a *Great Indian Festival* badge. The sale is live.
- **Google Immersive Product** (`more_stores=true`) shows **13 Indian stores at ₹669–₹1,299** (Zepto, bigbasket, boAt.com, …). Google's own price range is ₹799–999.
  - So the "80% discount" is measured against a list price nobody charges. ₹799 is simply the market price.
- **Google Shopping** has one seller at **₹199**, a 75% outlier below the market and a classic counterfeit/refurb signal.
- **Google Lens** on the Amazon listing photo returns the same product on **Flipkart, Myntra, Reliance Digital, AJIO and boAt.com with prices**. This gives Flipkart coverage although SerpApi has no Flipkart engine.
- **Entity problem:** Amazon's #1 result for the query was a *sponsored, different product*. Any comparison must first prove that all compared listings are the same product (and the same variant).

**Gap check against BuiltWithSerpApi (≈170 projects):**
- Plain price comparison is crowded: PriceScope, Price Check, PriceVerdict, MarketRadar.
- None targets India/amazon.in. None checks whether a claimed discount is real. None combines listing-photo provenance with cross-store price outliers.

## Round 1 — "Compare prices across Amazon.in and Google Shopping"
- **Objection:** at least 4 gallery projects already do this, and Keepa/BuyHatke exist. Low originality; nothing a judge has not seen.
- **Change:** stop being a comparison table. Answer the question shoppers actually ask during a sale: *is this deal real?*

## Round 2 — "Fake-discount detector"
- **Idea:** measure the claimed discount against the price the market actually charges.
- **Objection:** SerpApi returns *current* prices, not history. Without history, how can we call a discount fake?
- **Answer:** we don't need history to expose MRP theatre. A discount is only meaningful against a *reference price*. We can build that reference from today's cross-store market (the median of independent stores from Immersive Product with `more_stores`, plus Google's price range).
  - If the claimed MRP is far above anything the market charges, the "discount" is fictional, regardless of history.
  - We also persist every observation as a timestamped snapshot, so a local price history accumulates from the first run.
- **Change:** the core metric becomes **Real Discount = (market reference − deal price) / market reference**, shown beside the claimed discount, plus an **MRP Inflation ratio = claimed MRP / market reference**.

## Round 3 — add counterfeit / grey-market risk
- **Objection:** "cheapest" is not always "best". The ₹199 listing is the cheapest and almost certainly not genuine. A price tool that recommends it is harmful.
- **Answer:** use the same data for risk.
  - Prices far below the market band are flagged as outliers ("too cheap to be genuine").
  - Stores are classed as brand-official / major retailer / marketplace / unknown.
  - Lens on the listing photo shows where the *same image* appears (genuine retailers vs unknown shops).
- **Change:** the verdict recommends the **cheapest trustworthy offer**, not the cheapest offer. Outliers are shown with a warning, not recommended.

## Round 4 — same product, or a look-alike variant?
- **Objection:** the probe showed Amazon returning a different product as #1, and "Airdopes 141" covers Gen 2, ANC, Elite ANC and Pro. Comparing across variants produces fake insights, the very thing we criticise.
- **Answer:** make entity resolution a first-class step.
  - A deterministic pre-filter (brand, model tokens, variant words like ANC/Pro/Gen 2/storage/RAM) is followed by an LLM "same exact product?" judgement in one batched call.
  - Listings that fail are excluded and listed as "excluded: different variant", so the user can see why.
- **Change:** this is the technical heart of the agent, and it is where the AI earns its place.

## Round 5 — review truth
- **Objection:** discount and counterfeit checks still leave "is it any good?" Ratings on the store page are the least trusted signal (LocalCircles: 56% saw positively biased ratings).
- **Answer:** Immersive Product returns, in the same credit, a cross-store rating distribution, user reviews, **Reddit/forum discussions** and **YouTube review videos**.
  - The LLM summarises independent voices (forums and video titles) against the store rating and flags a gap when they disagree.
  - Every claim links to the source thread or video.
- **Change:** a "What real users say" panel with citations. No extra SerpApi credit.

## Round 6 — is it really an *agent*, and is SerpApi really core?
- **Objection:** a fixed pipeline is not an agent, and judges score "meaningful SerpApi usage".
- **Answer:**
  - **Input routing:** the agent picks its plan from the input. An Amazon URL → ASIN product lookup. A Flipkart/other URL → title from the URL slug, then search. Free text → search. An image URL → Lens first.
  - **Budgeting:** it spends credits only where needed. Lens runs only when the cross-store set is thin or the cheapest offer looks suspicious, and it has a hard per-verdict credit budget.
  - **Visible SerpApi trail:** every engine call is shown live in a timeline (engine, what it was for, results used). Without SerpApi there is nothing to show: no prices, stores, photos or reviews.
- **Change:** the UI streams the agent's plan and each SerpApi call over SSE.

## Round 7 — trust in the verdict itself
- **Objection:** LLM shopping assistants hallucinate prices and confidently recommend. One wrong number in a demo kills credibility.
- **Answer:**
  - **Numbers come from code, not the LLM.** Prices, medians, discounts and outlier flags are computed in code from SerpApi fields. The LLM is used only for entity matching and the plain-language narrative, and the narrative gets the computed numbers as input.
  - Every price shown carries its store, link and fetch time.
  - With fewer than 3 matching independent stores, the verdict says **"Not enough market data"** instead of guessing.

## Round 8 — adversarial judge review (independent critic agent)

An independent reviewer, briefed as a skeptical judge, re-checked our probe JSON and found real errors in our own evidence:

1. **We compared the wrong variant.**
   - The Immersive token we opened was Zepto's *original* Airdopes 141 (42 h). The Amazon card was **Gen 2** (48 h).
   - The correct Shopping card ("boAt Airdopes 141 Gen 2", sold by boAt) was in the same response at position 2.
   - → Entity resolution must run **before** choosing which product page to open, not after.
2. **"Fake discount" is the wrong frame and invites legal trouble.**
   - boAt's own store shows the same "80% off ₹3,990". MRP is printed by the brand under Legal Metrology rules as a *ceiling*.
   - → Neutral, numeric language only: "Claimed 80% off · saving vs today's market ₹X (Y%)", "MRP is 4.8× the highest store price", "priced far below market: verify the seller". We never say "fake" or "counterfeit".
3. **Data quality traps.**
   - `extracted_old_price` parses "80% off₹3,990" as **80**. We parse the `old_price` string ourselves and unit-test it.
   - Immersive has no per-store MRP.
   - Store lists contain gift resellers, EMI brokers and "contact for availability" rows.
   - Cheap results are often cases and skins.
   - Quick-commerce prices vary by pincode.
   - → Trimmed median, reseller and accessory filters, a "may vary by pincode" label, and "Not enough market data" below 3 clean stores.
4. **Lens evidence was for a different product** (we ran it on a sponsored thumbnail).
   - → Lens is demoted to an optional "Where else is this photo listed?" action on the *resolved* product image. Lens prices never enter the median.
5. **Honesty about scope.**
   - Without price history we cannot detect pre-sale hikes, so we don't claim to.
   - We expose MRP theatre and the real saving against today's market.
   - We call it a *verification pipeline with an LLM resolver*, not an autonomous agent.

## Round 9 — the killer screen

A judge should get the point in 20 seconds without reading. The core visual is a **price number line**:
- Every matched store is a dot. The market band is shaded.
- The deal price is highlighted inside the band.
- The MRP sits isolated far to the right, labelled *"MRP — no store charges this"*.
- Far-below-market listings are grey with "verify seller".

Headline: **"Claimed 80% OFF → your real saving today: ₹40 (5%)."**

## Round 10 — final idea

**AsliDaam** (असली दाम, "the real price"). Name checked: no existing Indian product found. We avoid "Sahi Daam" (NPPA's Pharma Sahi Daam app) and "Dekho" (PriceDekho).

> Paste an Amazon.in link or a product name during the festive sale. AsliDaam checks the same exact product across a dozen Indian stores via SerpApi, and tells you in 10 seconds:
> - what the market actually charges;
> - your real saving vs the claimed one;
> - how inflated the MRP is;
> - which stores are suspiciously cheap;
> - what independent users say.
>
> Every number is computed from live search data and linked to its source.

- **Track:** Commerce & Market Intelligence.
- **SerpApi engines:**
  - Amazon Search / Amazon Product (amazon.in): claimed price and MRP.
  - Google Shopping (gl=in): candidate product cards.
  - Google Immersive Product (`more_stores`): the cross-store market, rating distribution, user reviews, forum threads and YouTube reviews.
  - Google Lens (optional): photo provenance.
- **Hard budget:** 3 credits per verdict (+1 if the user asks for Lens), with a disk cache and replay mode.

## Round 11 — name

The working name "AsliDaam" (Hindi) was replaced with **Nijam** (நிஜம், Tamil for "real/truth"), tagline **நிஜ விலை · the real price**. No existing Indian price tool by that name was found.

Later renamed again to **Dealtective** ("the deal detective"). It is a catchier English play on words that any judge gets instantly, it says what the product does, and no existing app by that name was found.
