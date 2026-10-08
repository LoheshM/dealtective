// Nijam front end — no build step. Streams pipeline events over SSE and renders them.

const $ = (sel, root = document) => root.querySelector(sel);
const inr = (n) => (n == null || isNaN(n) ? "—" : "₹" + Math.round(n).toLocaleString("en-IN"));
const pct = (x) => (x == null ? "—" : Math.round(x * 100) + "%");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (typeof u === "string" && /^https?:\/\//i.test(u) ? u : null);
// For style="background-image:…": percent-encode everything that could close the url() or the attribute.
const cssUrl = (u) => {
  const ok = safeUrl(u);
  if (!ok) return "";
  const enc = ok.replace(/["'()\\\s<>]/g, (c) => "%" + c.charCodeAt(0).toString(16).padStart(2, "0"));
  return `url("${enc}")`;
};
const cssBg = (u) => { const v = cssUrl(u); return v ? esc(`background-image:${v}`) : ""; };

const ENGINE_NAMES = {
  amazon: "Amazon Search", amazon_product: "Amazon Product", google_shopping: "Google Shopping", google: "Google Search",
  google_immersive_product: "Immersive Product", google_lens: "Google Lens",
};
const LABELS = {
  good_deal: "Good deal", fair_price: "Fair price", above_market: "Above market",
  not_enough_data: "Not enough data", no_deal_price: "Market check", unusually_low: "Unusually low · verify seller",
};
const KIND = { official: "Official", major: "Major retailer", quick: "Quick commerce", other: "Other store", import: "International" };

let state;
let source;
let lensSource;

function reset() {
  state = { calls: [], credits: 0, matches: {}, anchor: null, market: null, verdict: null, offers: [], product: null, voices: null, summary: null, query: "" };
}

/* ---------- theme ---------- */
(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("nijam-theme"); } catch { /* storage may be blocked */ }
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("nijam-theme", next); } catch { /* ignore */ }
    if (state?.market) renderNumberLine();
  });
})();

/* ---------- status & examples ---------- */
async function loadStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    const mode = $("#pill-mode");
    if (s.mode === "replay") {
      mode.className = "pill replay";
      mode.innerHTML = '<span class="led"></span>Offline demo';
      mode.title = "Replay mode: recorded SerpApi responses, no credits used";
    } else {
      mode.className = "pill";
      mode.innerHTML = '<span class="led"></span>Live SerpApi';
    }
    const left = s.account?.searches_left;
    $("#pill-credits").textContent = left != null ? `${left} searches left` : "";
  } catch { /* status is informational */ }
}

async function loadExamples() {
  try {
    const ex = await (await fetch("/api/examples")).json();
    const box = $("#examples");
    box.innerHTML = "";
    for (const e of ex) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.innerHTML = `${esc(e.label)} <small>${esc(e.hint)}</small>`;
      b.addEventListener("click", () => { $("#q").value = e.query; start(e.query); });
      box.appendChild(b);
    }
  } catch { /* optional */ }
}

/* ---------- run ---------- */
$("#search-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const q = $("#q").value.trim();
  if (q.length >= 2) start(q);
});

function start(q) {
  if (source) source.close();
  if (lensSource) { lensSource.close(); lensSource = null; }
  reset();
  state.query = q;
  const url = new URL(location.href);
  url.searchParams.set("q", q);
  history.replaceState(null, "", url);

  $("#hero").classList.add("compact");
  $("#results").classList.remove("hidden");
  $("#go").classList.add("loading");
  $("#go").disabled = true;
  resetPanels();

  source = new EventSource("/api/check/stream?q=" + encodeURIComponent(q));
  const on = (name, fn) => source.addEventListener(name, (e) => { try { fn(JSON.parse(e.data)); } catch (err) { console.error(name, err); } });
  on("step", onStep);
  on("serp_call", onCall);
  on("notice", (d) => addNotice(d.text));
  on("anchor", onAnchor);
  on("match", onMatch);
  on("market", onMarket);
  on("voices", onVoices);
  on("summary", onSummary);
  on("lens", onLens);
  on("done", () => { finish(); loadStatus(); });
  // "error" fires both for server-sent error events (with data) and for connection failures (without).
  source.addEventListener("error", (e) => {
    let text = "Connection to the server was lost.";
    if (e.data) { try { text = JSON.parse(e.data).text || text; } catch { /* keep default */ } }
    else if (state.market) { finish(); return; } // stream ended after results: nothing to report
    addNotice(text, true);
    finish();
    if (!state.market) showError(text);
  });
}

function finish() {
  if (source) source.close();
  $("#go").classList.remove("loading");
  $("#go").disabled = false;
  document.querySelectorAll(".step.running").forEach((el) => el.classList.replace("running", "done"));
}

function resetPanels() {
  $("#steps").innerHTML = "";
  $("#credits-used").textContent = "0 credits";
  $("#product-img").className = "product-img skeleton";
  $("#product-img").style.backgroundImage = "";
  $("#product-badges").innerHTML = "";
  $("#product-title").textContent = "Reading listing…";
  $("#product-sub").textContent = "";
  $("#verdict-label").className = "verdict-label";
  $("#verdict-label").textContent = "Checking stores…";
  $("#headline").innerHTML = "";
  $("#tiles").innerHTML = "";
  $("#summary").innerHTML = "";
  $("#numberline").innerHTML = '<div class="skeleton block"></div>';
  $("#legend").innerHTML = "";
  $("#line-note").textContent = "";
  $("#stores").innerHTML = '<tbody><tr><td><div class="skeleton block small"></div></td></tr></tbody>';
  $("#stores-note").textContent = "";
  $("#ratings").innerHTML = "";
  $("#voices").innerHTML = '<li class="skeleton block small"></li>';
  $("#sources").innerHTML = "";
  $("#match-body").innerHTML = "";
  $("#match-note").textContent = "";
  $("#lens-grid").innerHTML = "";
  $("#lens-btn").disabled = true;
  $("#lens-btn").innerHTML = 'Run Google Lens <small>+1 credit</small>';
  $("#share-btn").disabled = true;
  $("#wa-btn").setAttribute("aria-disabled", "true");
  $("#wa-btn").removeAttribute("href");
}

function showError(text) {
  $("#verdict-label").className = "verdict-label above_market";
  $("#verdict-label").textContent = "Couldn't check";
  $("#headline").innerHTML = `<span class="error-box">${esc(text)}</span>`;
}

/* ---------- trail ---------- */
function onStep(d) {
  let li = document.getElementById("step-" + d.id);
  if (!li) {
    li = document.createElement("li");
    li.id = "step-" + d.id;
    li.innerHTML = '<div class="step-text"></div><div class="step-calls"></div>';
    $("#steps").appendChild(li);
  }
  li.className = "step " + (d.status === "warn" ? "warn" : d.status === "running" ? "running" : "done");
  $(".step-text", li).textContent = d.text;
}

function onCall(c) {
  state.calls.push(c);
  if (c.credit) state.credits += 1;
  $("#credits-used").textContent = `${state.credits} credit${state.credits === 1 ? "" : "s"} · ${state.calls.length} call${state.calls.length === 1 ? "" : "s"}`;
  const running = [...document.querySelectorAll(".step.running")].pop() || [...document.querySelectorAll(".step")].pop();
  const box = running ? $(".step-calls", running) : $("#steps");
  const tag = !c.ok ? '<span class="tag fail">failed</span>'
    : c.credit ? '<span class="tag live">live · 1 credit</span>'
    : `<span class="tag free">${c.source === "fixture" ? "recorded" : "cached"} · free</span>`;
  const div = document.createElement("div");
  div.className = "call";
  div.innerHTML = `<div class="call-top"><span class="engine ${esc(c.engine)}">${esc(ENGINE_NAMES[c.engine] || c.engine)}</span>${tag}<span class="call-ms">${c.ms} ms</span></div>
    <div class="call-why">${esc(c.purpose)}${c.result_count ? ` · ${c.result_count} result${c.result_count === 1 ? "" : "s"}` : ""}${c.error ? ` · ${esc(c.error)}` : ""}</div>`;
  box.appendChild(div);
}

function addNotice(text, isErr = false) {
  const n = document.createElement("div");
  n.className = "notice";
  if (isErr) n.style.color = "var(--bad)";
  n.textContent = text;
  $("#steps").appendChild(n);
}

/* ---------- anchor / product ---------- */
function onAnchor(a) {
  state.anchor = a;
  setProduct(a.title, a.image);
  const badges = $("#product-badges");
  badges.innerHTML = "";
  for (const b of a.badges || []) badges.insertAdjacentHTML("beforeend", `<span class="badge">${esc(b)}</span>`);
  if (a.bought) badges.insertAdjacentHTML("beforeend", `<span class="badge soft">${esc(a.bought)}</span>`);
  const bits = [];
  if (a.price) bits.push(`${a.store || "Amazon.in"} ${inr(a.price)}`);
  if (a.offer_price) bits.push(`${inr(a.offer_price)} with offers`);
  if (a.mrp) bits.push(`M.R.P. ${inr(a.mrp)}`);
  if (a.rating) bits.push(`★ ${a.rating}${a.reviews ? ` (${Number(a.reviews).toLocaleString("en-IN")})` : ""}`);
  if (a.seller) bits.push(`Sold by ${a.seller}`);
  $("#product-sub").textContent = bits.join(" · ") + (a.price_note ? ` · ${a.price_note}` : "");
}

function setProduct(title, image) {
  if (title) $("#product-title").textContent = title;
  const img = $("#product-img");
  if (safeUrl(image)) {
    img.classList.remove("skeleton");
    img.style.backgroundImage = cssUrl(image);
  }
}

/* ---------- matching ---------- */
function onMatch(d) {
  state.matches = { ...(state.matches || {}), [d.stage]: d.candidates };
  renderMatch();
}

function renderMatch() {
  const all = Object.values(state.matches || {}).flat();
  const count = (l) => all.filter((c) => c.label === l).length;
  const excluded = all.length - count("same");
  $("#match-note").textContent = `${count("same")} confirmed · ${excluded} excluded`;
  const stat = (n, l) => `<div class="mstat"><b>${n}</b>${l}</div>`;
  const order = { same: 0, variant: 1, different: 2, accessory: 3, reseller: 4 };
  const rows = [...all].sort((a, b) => (order[a.label] ?? 9) - (order[b.label] ?? 9) || (a.price || 0) - (b.price || 0));
  $("#match-body").innerHTML = `
    <p class="muted">Listings from Google Shopping and the product's store list are checked against the anchor title. Rules catch accessories, resellers and model-number mismatches, then an AI resolver labels the rest as <b>same</b>, <b>variant</b> or <b>different</b>. Only <b>same</b> listings count toward the market price.</p>
    <div class="match-stats">${stat(count("same"), "same product")}${stat(count("variant"), "other variant")}${stat(count("different"), "different product")}${stat(count("accessory"), "accessories")}${stat(count("reseller"), "gift/EMI resellers")}</div>
    <ul class="mlist">${rows.map((c) => `<li><span class="mlabel ${esc(c.label)}">${esc(c.label)}</span>
      <span class="mt" title="${esc(c.title)}">${esc(c.store)} · ${esc(c.title)}<span class="mr">${esc(c.reason)}</span></span>
      <span class="price" style="font-size:13px">${inr(c.price)}</span></li>`).join("")}</ul>`;
}

/* ---------- market & verdict ---------- */
function onMarket(d) {
  state.market = d.market;
  state.verdict = d.verdict;
  state.offers = d.offers;
  state.product = d.product;
  if (!state.anchor) setProduct(d.product?.title || state.query, d.product?.image);
  else if (!safeUrl(state.anchor.image)) setProduct(null, d.product?.image);
  renderVerdict();
  renderNumberLine();
  renderStores();
  $("#lens-btn").disabled = !safeUrl(state.anchor?.image || d.product?.image);
  $("#share-btn").disabled = false;
  const wa = $("#wa-btn");
  wa.href = "https://wa.me/?text=" + encodeURIComponent(shareText());
  wa.setAttribute("aria-disabled", "false");
}

function renderVerdict() {
  const v = state.verdict, m = state.market;
  const lab = $("#verdict-label");
  lab.className = "verdict-label " + v.label;
  lab.textContent = LABELS[v.label] || v.label;

  let head;
  if (v.label === "not_enough_data") {
    head = `Too few stores sell this exact product to judge the price${v.claimed_discount ? ` (the listing claims ${pct(v.claimed_discount)} off)` : ""}.`;
  } else if (v.label === "no_deal_price") {
    head = `Market price today: <b>${inr(m.reference)}</b> across ${m.store_count} stores`;
  } else {
    const rs = v.real_saving;
    const real = rs > 0.005 ? `real saving <b>${inr(v.real_saving_abs)}</b> (${pct(rs)})`
      : rs < -0.005 ? `<b>${inr(-v.real_saving_abs)}</b> above the market price`
      : `real saving <b>₹0</b>, the same as the market price`;
    head = v.claimed_discount
      ? `Claimed <span class="strike">${pct(v.claimed_discount)} off</span><span class="arrow">→</span>${real}`
      : real.charAt(0).toUpperCase() + real.slice(1);
  }
  const ctx = [];
  if (v.cheaper_at) ctx.push(`<b>${inr(v.cheaper_at.price)}</b> at ${esc(v.cheaper_at.store)}: cheaper than this listing`);
  else if (v.same_price_at?.length) ctx.push(`Same price at ${v.same_price_at.map(esc).join(", ")}`);
  $("#headline").innerHTML = head + (ctx.length ? `<span class="context">${ctx.join(" · ")}</span>` : "");

  const tiles = [];
  if (v.deal_price != null) {
    tiles.push(tile("This listing", inr(v.deal_price), v.mrp ? `M.R.P. ${inr(v.mrp)}${v.claimed_discount ? ` (−${pct(v.claimed_discount)})` : ""}` : (state.anchor?.store || "Amazon.in")));
  }
  tiles.push(tile("Market price", m.reference != null ? inr(m.reference) : "—",
    m.status === "ok" ? `median of ${m.store_count} in-stock ${m.basis === "major" ? "major retailers" : "stores"}` : `${m.store_count} matching store${m.store_count === 1 ? "" : "s"} (need 3)`));
  if (v.real_saving != null) {
    tiles.push(tile("Real saving", v.real_saving_abs > 0 ? inr(v.real_saving_abs) : v.real_saving_abs < 0 ? "−" + inr(-v.real_saving_abs) : "₹0",
      v.real_saving_abs < 0 ? "you'd pay above market" : `${pct(Math.max(0, v.real_saving))} vs market`, false));
  } else if (m.min_price != null) {
    tiles.push(tile("Price range", `${inr(m.min_price)}–${inr(m.max_price)}`, "matching stores"));
  }
  if (v.mrp_multiple != null) {
    tiles.push(tile("M.R.P. vs market", `${v.mrp_multiple.toFixed(1)}×`, v.mrp_theatre ? `no store charges ${inr(v.mrp)}` : "M.R.P. ÷ market price", v.mrp_theatre));
  } else if (v.best_trusted) {
    tiles.push(tile("Best trusted price", inr(v.best_trusted.price), v.best_trusted.store));
  }
  $("#tiles").innerHTML = tiles.join("");
}

function tile(k, v, s, hot = false) {
  const long = String(v).length > 9 ? " long" : "";
  return `<div class="tile${hot ? " hot" : ""}${long}"><div class="k">${esc(k)}</div><div class="v" title="${esc(v)}">${esc(v)}</div><div class="s">${esc(s)}</div></div>`;
}

/* ---------- the number line ---------- */
function renderNumberLine() {
  const el = $("#numberline");
  const m = state.market, v = state.verdict;
  const offers = state.offers.filter((o) => !o.is_anchor);
  const prices = offers.map((o) => o.price);
  if (v.deal_price) prices.push(v.deal_price);
  if (v.mrp) prices.push(v.mrp);
  if (!prices.length) { el.innerHTML = '<p class="muted">No prices to plot.</p>'; return; }

  // Size the drawing to the container so labels stay legible on phones.
  const W = Math.max(340, Math.min(760, Math.round(el.clientWidth || 760))), H = 210, L = 18, R = 18, AX = 158;
  const maxP = Math.max(...prices) * 1.06;
  const x = (p) => L + (p / maxP) * (W - L - R);
  const parts = [];

  // ticks
  const step = niceStep(maxP / 5);
  for (let t = 0; t <= maxP; t += step) {
    parts.push(`<g class="tick"><line x1="${x(t)}" x2="${x(t)}" y1="${AX}" y2="${AX + 5}" stroke="var(--line)" stroke-width="2"/>
      <text x="${x(t)}" y="${AX + 20}" text-anchor="middle">${t === 0 ? "₹0" : compactInr(t)}</text></g>`);
  }
  // market band
  if (m.status === "ok") {
    const bx = x(m.band_low), bw = Math.max(6, x(m.band_high) - x(m.band_low));
    parts.push(`<rect class="band" x="${bx - 3}" y="40" width="${bw + 6}" height="${AX - 40}" rx="8"/>`);
    parts.push(`<text class="band-lbl" x="${bx - 3}" y="33">Market ${inr(m.reference)}</text>`);
  }
  parts.push(`<line class="axis" x1="${L}" x2="${W - R}" y1="${AX}" y2="${AX}"/>`);

  // MRP marker and the gap to the market
  if (v.mrp) {
    const mx = x(v.mrp);
    const anchorEnd = mx > W - 160;
    parts.push(`<line class="mrp-line" x1="${mx}" x2="${mx}" y1="18" y2="${AX}"/>`);
    parts.push(`<text class="mrp-lbl" x="${anchorEnd ? mx - 6 : mx + 6}" y="26" text-anchor="${anchorEnd ? "end" : "start"}">M.R.P. ${inr(v.mrp)}</text>`);
    if (v.mrp_theatre) {
      parts.push(`<text class="lbl" x="${anchorEnd ? mx - 6 : mx + 6}" y="42" text-anchor="${anchorEnd ? "end" : "start"}">no store charges this</text>`);
    }
    if (m.reference && v.mrp_multiple && v.mrp > m.reference * 1.2) {
      const rx = x(m.reference) + 8, y = 96;
      parts.push(`<line class="gap" x1="${rx}" x2="${mx - 6}" y1="${y}" y2="${y}" marker-end="url(#arr)"/>`);
      parts.push(`<text class="gap-lbl" x="${(rx + mx) / 2}" y="${y - 7}" text-anchor="middle">${W < 520 ? "" : "M.R.P. is "}${v.mrp_multiple.toFixed(1)}× the market price</text>`);
    }
  }

  // store dots, stacked when they collide
  const rows = [];
  const placed = [...offers].sort((a, b) => a.price - b.price).map((o) => {
    const cx = x(o.price);
    let r = 0;
    while (r < 7 && (rows[r] || []).some((px) => Math.abs(px - cx) < 15)) r++;
    (rows[r] = rows[r] || []).push(cx);
    return { o, cx, cy: AX - 11 - r * 15 };
  });
  placed.forEach(({ o, cx, cy }, i) => {
    const trusted = ["official", "major", "quick"].includes(o.kind);
    const cls = ["dot", trusted ? "trusted" : "", o.flag ? "flag" : "", o.available ? "" : "unavailable"].join(" ");
    parts.push(`<circle class="${cls}" cx="${cx}" cy="${cy}" r="6" data-i="${i}"/>`);
  });
  // the listing being judged
  if (v.deal_price) {
    const dx = x(v.deal_price);
    parts.push(`<line x1="${dx}" x2="${dx}" y1="56" y2="${AX}" stroke="var(--accent)" stroke-width="2.5"/>`);
    parts.push(`<circle class="deal" cx="${dx}" cy="${AX}" r="8"/>`);
    const right = dx > W - 150;
    parts.push(`<text class="deal-lbl" x="${right ? dx - 6 : dx + 6}" y="64" text-anchor="${right ? "end" : "start"}">This listing ${inr(v.deal_price)}</text>`);
  }

  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Number line of store prices with market band, this listing and M.R.P.">
    <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--bad)"/></marker></defs>
    ${parts.join("")}</svg><div class="tip hidden" id="tip"></div>`;

  const tip = $("#tip");
  el.querySelectorAll("circle.dot").forEach((c) => {
    const { o } = placed[+c.dataset.i];
    const show = () => {
      const svg = el.querySelector("svg").getBoundingClientRect();
      const s = svg.width / W;
      tip.innerHTML = `<b>${esc(o.store)}</b> · ${inr(o.price)}${o.available ? "" : " · out of stock"}${o.flag === "low_outlier" ? " · far below market" : ""}`;
      tip.style.left = (+c.getAttribute("cx")) * s + "px";
      tip.style.top = (+c.getAttribute("cy")) * s + "px";
      tip.classList.remove("hidden");
    };
    c.addEventListener("mouseenter", show);
    c.addEventListener("focus", show);
    c.addEventListener("mouseleave", () => tip.classList.add("hidden"));
    c.addEventListener("click", () => { const u = safeUrl(o.link); if (u) window.open(u, "_blank", "noopener"); });
  });

  $("#legend").innerHTML = `
    <span><i class="lg trusted"></i>Official / major / quick-commerce store</span>
    <span><i class="lg other"></i>Other store</span>
    <span><i class="lg flag"></i>Far from market (verify)</span>
    ${v.deal_price ? '<span><i class="lg deal"></i>This listing</span>' : ""}
    ${m.status === "ok" ? '<span><i class="lg band"></i>Middle 50% of prices</span>' : ""}
    ${v.mrp ? '<span><i class="lg mrp"></i>M.R.P.</span>' : ""}`;
  $("#line-note").textContent = `${offers.length} matching listing${offers.length === 1 ? "" : "s"}`;
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const k of [1, 2, 2.5, 5, 10]) if (raw <= k * p) return k * p;
  return 10 * p;
}
function compactInr(n) {
  if (n >= 1e7) return "₹" + +(n / 1e7).toFixed(1) + "Cr";
  if (n >= 1e5) return "₹" + +(n / 1e5).toFixed(1) + "L";
  if (n >= 1e3) return "₹" + +(n / 1e3).toFixed(1) + "k";
  return "₹" + Math.round(n);
}

/* ---------- stores table ---------- */
function renderStores() {
  const v = state.verdict;
  const best = v.best_trusted;
  const rows = state.offers.map((o) => {
    const flags = [];
    if (o.is_anchor) flags.push('<span class="flagchip you">This listing</span>');
    if (best && !o.is_anchor && best.store === o.store && best.price === o.price) flags.push('<span class="flagchip best">Best trusted price</span>');
    if (o.flag === "low_outlier") flags.push('<span class="flagchip warn">Far below market · verify seller</span>');
    if (o.flag === "high_outlier") flags.push('<span class="flagchip warn">Well above market</span>');
    if (!o.available) flags.push('<span class="flagchip">Out of stock</span>');
    if (!o.in_reference && !o.is_anchor && o.kind !== "import") flags.push('<span class="flagchip">Same marketplace · not in market price</span>');
    for (const n of o.notes || []) {
      if (/pincode|international/i.test(n)) flags.push(`<span class="flagchip${/international/i.test(n) ? " warn" : ""}">${esc(n)}</span>`);
      else if (/^sold by/i.test(n)) flags.push(`<span class="flagchip">${esc(n)}</span>`);
    }
    const logo = safeUrl(o.logo)
      ? `<span class="logo" style="${cssBg(o.logo)}"></span>`
      : `<span class="logo">${esc((o.store || "?").slice(0, 1).toUpperCase())}</span>`;
    const rating = Number(o.rating) ? `★ ${esc(Number(o.rating))}${Number(o.reviews) ? ` <small>(${Number(o.reviews).toLocaleString("en-IN")})</small>` : ""}` : '<small class="muted">no rating</small>';
    const link = safeUrl(o.link) ? `<a class="visit" href="${esc(o.link)}" target="_blank" rel="noopener">Visit ↗</a>` : "";
    return `<tr class="${o.is_anchor ? "anchor" : ""} ${o.available ? "" : "dim"}">
      <td><div class="store-cell">${logo}<div><div class="store-name">${esc(o.store)}</div><div class="store-title" title="${esc(o.title)}">${esc(o.title)}</div></div></div></td>
      <td class="price">${inr(o.price)}</td>
      <td><span class="kind ${esc(o.kind)}">${esc(KIND[o.kind] || o.kind)}</span></td>
      <td class="rating">${rating}</td>
      <td><div class="flags">${flags.join("")}</div></td>
      <td>${link}</td></tr>`;
  });
  $("#stores").innerHTML = `<thead><tr><th>Store</th><th>Price</th><th>Type</th><th>Store rating</th><th>Notes</th><th></th></tr></thead><tbody>${rows.join("") || '<tr><td colspan="6" class="muted">No matching stores found.</td></tr>'}</tbody>`;
  const n = state.offers.filter((o) => !o.is_anchor).length;
  $("#stores-note").textContent = `${n} verified listing${n === 1 ? "" : "s"} · sorted by price`;
}

/* ---------- voices ---------- */
function onVoices(d) {
  state.voices = d;
  const box = $("#ratings");
  const dist = (Array.isArray(d.ratings) ? d.ratings : [])
    .map((r) => ({ stars: Number(r?.stars) || 0, amount: Number(r?.amount) || 0 }))
    .filter((r) => r.stars >= 1 && r.stars <= 5)
    .sort((a, b) => b.stars - a.stars);
  const total = dist.reduce((s, r) => s + r.amount, 0);
  const rating = Number(d.rating);
  if (rating || total) {
    box.innerHTML = `<div class="big">${rating ? esc(rating) : "—"}<small> / 5</small></div>
      <div class="muted">${(total || Number(d.reviews) || 0).toLocaleString("en-IN")} ratings across stores</div>
      <div class="bars">${dist.map((r) => `<div class="bar"><span>${r.stars}★</span><span class="track"><span class="fill" style="width:${total ? (100 * r.amount / total).toFixed(1) : 0}%"></span></span><span>${r.amount.toLocaleString("en-IN")}</span></div>`).join("")}</div>`;
  } else {
    box.innerHTML = '<p class="muted">No cross-store rating data.</p>';
  }
  const srcs = d.sources || [];
  const cards = srcs.filter((s) => s.type === "video" || s.type === "forum").map((s) => {
    const u = safeUrl(s.link);
    const thumb = s.type === "video" && safeUrl(s.thumbnail)
      ? `<span class="thumb" style="${cssBg(s.thumbnail)}"></span>`
      : `<span class="thumb">${s.type === "video" ? "▶" : "💬"}</span>`;
    return `<a class="src" ${u ? `href="${esc(u)}" target="_blank" rel="noopener"` : ""} id="src-${s.id}">${thumb}
      <span><span class="n">[${s.id + 1}]</span> <span class="t">${esc(s.title)}</span><span class="m">${esc(s.source || "")}${s.meta ? " · " + esc(s.meta) : ""}</span></span></a>`;
  });
  $("#sources").innerHTML = cards.join("");
  state.storeSummary = srcs.find((s) => s.type === "store_summary");
}

function onSummary(d) {
  state.summary = d;
  $("#summary").innerHTML = d.summary ? `<span class="ai">${d.generated_by === "llm" ? "AI summary of computed numbers" : "Summary"}</span>${esc(d.summary)}` : "";
  const srcs = state.voices?.sources || [];
  const byId = Object.fromEntries(srcs.map((s) => [s.id, s]));
  const icon = { positive: "+", negative: "−", mixed: "±" };
  const list = (d.voices || []).map((vb) => {
    const cites = vb.source_ids.map((i) => {
      const s = byId[i];
      const u = safeUrl(s?.link);
      return u ? `<a href="${esc(u)}" target="_blank" rel="noopener" title="${esc(s.title || s.source || "")}">${i + 1}</a>` : `<a title="${esc(s?.title || "")}">${i + 1}</a>`;
    }).join("");
    return `<li class="voice"><span class="tone ${esc(vb.tone)}">${icon[vb.tone] || "•"}</span><span>${esc(vb.point)}<span class="cite">${cites}</span></span></li>`;
  });
  $("#voices").innerHTML = list.join("") || '<li class="muted">Not enough independent user content was found for this product.</li>';
  if (state.storeSummary) {
    $("#voices").insertAdjacentHTML("beforeend", `<li class="store-summary"><b>Amazon.in's own review summary:</b> ${esc(state.storeSummary.text)}</li>`);
  }
}

/* ---------- lens ---------- */
$("#lens-btn").addEventListener("click", () => {
  const image = safeUrl(state.anchor?.image || state.product?.image);
  if (!image) return;
  const btn = $("#lens-btn");
  btn.disabled = true;
  btn.innerHTML = 'Searching… <small>Google Lens</small>';
  $("#lens-grid").innerHTML = '<div class="skeleton block small"></div>';
  if (lensSource) lensSource.close();
  const es = lensSource = new EventSource(`/api/lens/stream?image=${encodeURIComponent(image)}&title=${encodeURIComponent(state.anchor?.title || state.product?.title || "")}`);
  const done = () => { es.close(); btn.innerHTML = 'Run Google Lens <small>done</small>'; loadStatus(); };
  es.addEventListener("serp_call", (e) => onCall(JSON.parse(e.data)));
  es.addEventListener("lens", (e) => onLens(JSON.parse(e.data)));
  es.addEventListener("error", (e) => {
    let text = "Lens request failed.";
    if (e.data) { try { text = JSON.parse(e.data).text || text; } catch { /* keep default */ } }
    $("#lens-grid").innerHTML = `<p class="muted">${esc(text)}</p>`;
    done();
  });
  es.addEventListener("done", done);
});

function onLens(d) {
  const items = (d.matches || []).map((mt) => {
    const u = safeUrl(mt.link);
    const img = cssBg(mt.thumbnail);
    return `<a class="lens-item" ${u ? `href="${esc(u)}" target="_blank" rel="noopener"` : ""}>
      <div class="img" style="${img}"></div>
      <div class="b"><div class="src-name">${esc(mt.store)} ${mt.price ? `· ${inr(mt.price)}` : ""}</div>
      <div class="ttl">${esc(mt.title)}</div>
      <span class="mlabel ${esc(mt.label)}" style="display:inline-block;margin-top:6px">${esc(mt.label)}</span>
      <span class="kind ${esc(mt.kind)}">${esc(KIND[mt.kind] || "")}</span></div></a>`;
  });
  $("#lens-grid").innerHTML = items.join("") || '<p class="muted">Lens found no matches.</p>';
}

/* ---------- share ---------- */
function shortTitle(t) { return (t || "").split(/[,|(]/)[0].trim().slice(0, 70); }
function shareText() {
  const v = state.verdict, m = state.market;
  const t = shortTitle(state.anchor?.title || state.product?.title || state.query);
  const lines = [`Nijam check: ${t}`];
  if (v.deal_price) lines.push(`${state.anchor?.store || "Amazon.in"}: ${inr(v.deal_price)}${v.claimed_discount ? ` (claims ${pct(v.claimed_discount)} off M.R.P. ${inr(v.mrp)})` : ""}`);
  if (m.status === "ok") lines.push(`Market price across ${m.store_count} Indian stores: ${inr(m.reference)}`);
  if (v.real_saving != null) lines.push(v.real_saving_abs >= 0 ? `Real saving vs market: ${inr(v.real_saving_abs)}` : `That's ${inr(-v.real_saving_abs)} above market`);
  if (v.mrp_theatre) lines.push(`M.R.P. is ${v.mrp_multiple.toFixed(1)}× what stores actually charge`);
  if (v.best_trusted) lines.push(`Best trusted price: ${inr(v.best_trusted.price)} at ${v.best_trusted.store}`);
  lines.push(location.href);
  return lines.join("\n");
}
$("#share-btn").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(shareText()); toast("Copied: paste it into WhatsApp"); }
  catch { toast("Couldn't access the clipboard"); }
});
function toast(t) {
  const el = $("#toast");
  el.textContent = t;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 2200);
}

let resizeT;
addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (state?.market) renderNumberLine(); }, 150); });

/* ---------- boot ---------- */
reset();
loadStatus();
loadExamples();
const initial = new URL(location.href).searchParams.get("q");
if (initial) { $("#q").value = initial; start(initial); }
