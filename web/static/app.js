/* Ignition & Coil dashboard. Vanilla JS, no build step; all DOM built with textContent (no HTML injection). */
"use strict";

const TZ = "Europe/London";
const S = { data: null, prices: {}, sort: loadPref("sort", { key: "score", dir: -1 }), filter: "", view: null };

// ---------------------------------------------------------------- utilities
function loadPref(k, d) { try { const v = localStorage.getItem("ic." + k); return v ? JSON.parse(v) : d; } catch { return d; } }
function savePref(k, v) { try { localStorage.setItem("ic." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } }

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}
const isNum = (x) => typeof x === "number" && isFinite(x);
const fx = (x, d = 2) => (isNum(x) ? x.toFixed(d) : "–");
const sgn = (x, d = 2, suf = "") => (isNum(x) ? (x > 0 ? "+" : "") + x.toFixed(d) + suf : "–");
function price(x, p) {
  if (!isNum(x)) return "–";
  if (p == null) p = x >= 1000 ? 1 : x >= 1 ? Math.max(2, 5 - Math.floor(Math.log10(x))) : Math.min(8, 4 - Math.floor(Math.log10(x)));
  return x.toLocaleString("en-GB", { minimumFractionDigits: Math.max(0, p), maximumFractionDigits: Math.max(0, p) });
}
const dtFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const tFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit" });
const hmFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
const dFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, day: "2-digit", month: "short" });
const when = (ms) => (ms ? dtFmt.format(new Date(ms)) : "–");
const clock = (ms) => (ms ? tFmt.format(new Date(ms)) : "–");
const cls = (x, good, bad) => (!isNum(x) ? "faint" : good(x) ? "pos" : bad && bad(x) ? "neg" : "");

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = "/login"; throw new Error("login required"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

// ---------------------------------------------------------------- websocket
let wsConn = null;
let wsRetry = 1000;
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  wsConn = new WebSocket(`${proto}://${location.host}/ws`);
  wsConn.onopen = () => { wsRetry = 1000; };
  wsConn.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "state") { S.data = m; renderHeader(); if (S.view && S.view.onState) S.view.onState(m); }
    else if (m.type === "prices") {
      S.prices = m.data;
      if (S.data && m.header && !m.header.starting) { S.data.header = m.header; renderHeader(); }
      if (S.view && S.view.onPrices) S.view.onPrices(m);
    }
  };
  wsConn.onclose = () => {
    renderHeader(true);
    setTimeout(connect, wsRetry);
    wsRetry = Math.min(wsRetry * 2, 15000);
  };
}

// ---------------------------------------------------------------- header
function renderHeader(disconnected) {
  const el = document.getElementById("status");
  el.replaceChildren();
  if (disconnected || !S.data) { el.append(h("span", { class: "chip bad", text: "dashboard disconnected – retrying" })); return; }
  const x = S.data.header;
  if (x.starting) { el.append(h("span", { class: "chip warn", text: "Starting – building universe and loading history…" })); return; }
  const reg = x.regime || "n/a";
  const regCls = reg === "RISK_ON" ? "good" : reg === "RISK_OFF" ? "bad" : "warn";
  el.append(h("span", { class: `chip ${regCls}`, title: "Market regime" }, reg + (x.btc_dump ? " · BTC_DUMP" : "")));
  el.append(h("span", { class: "chip", title: `BTC ${x.btc_above_ema50 ? "above" : "below"} 1h EMA50 (${x.btc_ema50_rising ? "rising" : "falling"})` },
    "BTC ", h("span", { class: "num", text: price(x.btc_price, 1) }), " ",
    h("span", { class: "num " + cls(x.btc_ret_1h, (v) => v > 0, (v) => v < 0), text: sgn(x.btc_ret_1h, 2, "%") })));
  el.append(h("span", { class: "chip", title: "% of universe with 1h close above EMA20" }, "Breadth ", h("span", { class: "num", text: fx(x.breadth, 0) + "%" })));
  const ok = x.data_ok && x.ws_ok;
  el.append(h("span", { class: `chip ${ok ? "good" : "bad"}`, title: `Data ${x.data_ok ? "OK" : "STALE"} · WebSocket ${x.ws_ok ? "OK" : "down"}` },
    h("i", { class: "dot" }), ok ? "Data OK" : !x.data_ok ? "Data STALE" : "WS down"));
  el.append(h("span", { class: "chip muted", title: "Last scan (London)" }, "Scan ", h("span", { class: "num", text: clock(x.last_scan_ms) })));
  if (x.needs_confirmation) el.append(h("a", { class: "chip bad", href: "/trades", "data-link": true, title: "Trades where price crossed your stop or a target and you haven't logged it" }, `${x.needs_confirmation} to confirm`));
  if (x.open_trades) el.append(h("a", { class: "chip info", href: "/trades", "data-link": true }, `${x.open_trades} open trade${x.open_trades > 1 ? "s" : ""}`));
  if (x.paused) el.append(h("span", { class: "chip warn", text: "Signals paused" }));
  if (!x.telegram) el.append(h("span", { class: "chip muted", title: "Set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env", text: "Telegram off" }));
}

// ---------------------------------------------------------------- router
const routes = [
  [/^\/$/, scannerView],
  [/^\/signals$/, signalsView],
  [/^\/signal\/(S-\d+)$/, signalView],
  [/^\/symbol\/([A-Z0-9]+)$/, symbolView],
  [/^\/settings$/, settingsView],
];   // journal.js adds the trade / stats routes
function navigate(path, push = true) {
  if (push) history.pushState({}, "", path);
  if (S.view && S.view.destroy) S.view.destroy();
  const app = document.getElementById("app");
  app.replaceChildren();
  window.scrollTo(0, 0);
  for (const a of document.querySelectorAll(".nav a")) a.classList.toggle("active", a.getAttribute("href") === path);
  for (const [re, view] of routes) {
    const m = path.match(re);
    if (m) { S.view = view(app, ...m.slice(1)) || null; return; }
  }
  app.append(h("div", { class: "empty", text: "Page not found." }));
  S.view = null;
}
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-link]");
  if (a && !e.ctrlKey && !e.metaKey && !e.shiftKey && e.button === 0) { e.preventDefault(); navigate(a.getAttribute("href")); }
});
window.addEventListener("popstate", () => navigate(location.pathname, false));
const link = (href, text, c) => h("a", { href, "data-link": true, class: c, text });

// ---------------------------------------------------------------- scanner
const COLS = [
  { key: "symbol", label: "Symbol", left: true, fmt: (r) => link(`/symbol/${r.symbol}`, r.symbol + (r.warm ? "" : " *")) },
  { key: "price", label: "Price", fmt: (r) => h("span", { class: "num", "data-price": r.symbol, "data-prec": r.precision ?? "", text: price(livePrice(r.symbol) ?? r.price, r.precision) }) },
  { key: "state", label: "State", fmt: (r) => h("span", { class: `badge ${r.state}`, text: r.state }) },
  { key: "score", label: "Score", fmt: (r) => h("span", { class: "num" }, String(r.score),
      h("span", { class: "scorebar" + (r.score >= 70 ? " hi" : "") }, h("i", { style: `width:${Math.max(0, Math.min(100, r.score))}%` }))) },
  { key: "n_pass", label: "Pass", title: "Active conditions met for the best setup (switched-off ones not counted)", fmt: (r) => h("span", { class: "num muted", text: `${r.n_pass}/${r.n_conds} ${r.setup === "COIL" ? "C" : "I"}` }) },
  { key: "ret_1h", label: "1h %", fmt: (r) => num(r.ret_1h, sgn(r.ret_1h), cls(r.ret_1h, (v) => v >= 1 && v <= 5, (v) => v < 0)) },
  { key: "rvol_5m", label: "RVOL 5m", fmt: (r) => num(r.rvol_5m, fx(r.rvol_5m, 1) + "x", cls(r.rvol_5m, (v) => v >= 3)) },
  { key: "rvol_15m", label: "RVOL 15m", fmt: (r) => num(r.rvol_15m, fx(r.rvol_15m, 1) + "x", cls(r.rvol_15m, (v) => v >= 2)) },
  { key: "rs_1h", label: "RS 1h", fmt: (r) => num(r.rs_1h, sgn(r.rs_1h), cls(r.rs_1h, (v) => v >= 1, (v) => v < 0)) },
  { key: "oi_chg_1h", label: "OI Δ1h", fmt: (r) => num(r.oi_chg_1h, sgn(r.oi_chg_1h, 2, "%"), cls(r.oi_chg_1h, (v) => v >= 2, (v) => v < 0)) },
  { key: "funding_8h", label: "Fund 8h", fmt: (r) => num(r.funding_8h, sgn(r.funding_8h, 4, "%"), cls(r.funding_8h, (v) => v <= 0.01, (v) => v > 0.03)) },
  { key: "taker", label: "Taker", fmt: (r) => num(r.taker, fx(r.taker, 2), cls(r.taker, (v) => v >= 0.55, (v) => v < 0.45)) },
  { key: "headroom_pct", label: "Room", fmt: (r) => num(r.headroom_pct, fx(r.headroom_pct, 1) + "%" + (r.price_discovery ? " ↑" : ""),
      cls(r.headroom_pct, (v) => v >= 4, (v) => v < 2)) },
  { key: "wicks", label: "Wicks", title: "Deep lower wicks in the last 24h (wick risk)", fmt: (r) => num(r.wicks, String(r.wicks), cls(r.wicks, () => false, (v) => v > 3)) },
];
function num(v, text, c) { return h("span", { class: "num " + (c || ""), text: isNum(v) ? text : "–" }); }
function livePrice(sym) { const p = S.prices[sym]; return p ? (p.last ?? p.mark) : null; }

function scannerView(app) {
  const search = h("input", { type: "search", placeholder: "Filter symbol…", value: S.filter, "aria-label": "Filter symbols",
    oninput: (e) => { S.filter = e.target.value.toUpperCase(); draw(); } });
  const count = h("span", { class: "muted small" });
  const wrap = h("div", { class: "table-wrap" });
  const sigBox = h("div");
  app.append(h("div", { class: "stack" },
    h("div", {}, h("div", { class: "toolbar" }, h("h2", { text: "Scanner", style: "margin:0" }), search, count),
      wrap, h("p", { class: "faint small", text: "Click a column to sort. * = still warming up. Room ↑ = price discovery (no resistance in the lookback). Hover a row for failed conditions." })),
    h("div", { class: "card" }, h("h2", { text: "Recent signals (7 days)" }), sigBox)));

  function draw() {
    if (!S.data || S.data.header.starting) {
      wrap.replaceChildren(h("div", { class: "empty", text: "Starting up: building the universe and loading history (about 30 seconds)…" }));
      return;
    }
    let rows = S.data.rows.filter((r) => !S.filter || r.symbol.includes(S.filter));
    const { key, dir } = S.sort;
    const rank = { ENTRY: 2, WATCH: 1, SKIP: 0 };
    rows.sort((a, b) => {
      let x = a[key], y = b[key];
      if (key === "state") { x = rank[x]; y = rank[y]; }
      if (typeof x === "string") return dir * x.localeCompare(y);
      x = isNum(x) ? x : -Infinity; y = isNum(y) ? y : -Infinity;
      return dir * (x - y) || b.score - a.score;
    });
    count.textContent = `${rows.length} of ${S.data.rows.length} symbols`;
    const thead = h("tr", {}, COLS.map((c) => h("th", {
      class: "sortable" + (c.left ? " left" : ""), title: c.title || "Sort by " + c.label, scope: "col",
      onclick: () => { S.sort = { key: c.key, dir: S.sort.key === c.key ? -S.sort.dir : (c.key === "symbol" ? 1 : -1) }; savePref("sort", S.sort); draw(); },
    }, c.label, S.sort.key === c.key ? h("span", { class: "arrow", text: S.sort.dir > 0 ? "▲" : "▼" }) : null)));
    const body = rows.map((r) => h("tr", {
      class: `clickable state-${r.state}`, title: r.failed && r.failed.length ? "Failed: " + r.failed.join(", ") : "All conditions pass",
      onclick: (e) => { if (!e.target.closest("a")) navigate(`/symbol/${r.symbol}`); },
    }, COLS.map((c) => h("td", { class: c.left ? "left" : "" }, c.fmt(r)))));
    wrap.replaceChildren(h("table", {}, h("thead", {}, thead), h("tbody", {}, body)));
    if (!rows.length) wrap.append(h("div", { class: "empty", text: "No symbols match." }));
    sigBox.replaceChildren(signalTable(S.data.signals, "No signals in the last 7 days."));
  }
  draw();
  return {
    onState: draw,
    onPrices: () => { for (const el of wrap.querySelectorAll("[data-price]")) { const p = livePrice(el.dataset.price); if (isNum(p)) el.textContent = price(p, el.dataset.prec === "" ? null : +el.dataset.prec); } },
  };
}

function signalTable(list, emptyText) {
  if (!list || !list.length) return h("div", { class: "empty", text: emptyText });
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["Signal", "Time (London)", "Setup", "Symbol", "Score", "Regime", "Session", "Alert"].map((t, i) => h("th", { class: i < 4 ? "left" : "", text: t })))),
    h("tbody", {}, list.map((s) => h("tr", { class: "clickable", onclick: (e) => { if (!e.target.closest("a")) navigate(`/signal/${s.signal_id}`); } },
      h("td", { class: "left" }, link(`/signal/${s.signal_id}`, s.signal_id)),
      h("td", { class: "left num", text: when(s.bar_close_ms) }),
      h("td", { class: "left" }, h("span", { class: `badge ${s.setup}`, text: s.setup })),
      h("td", { class: "left", text: s.symbol }),
      h("td", { class: "num", text: String(s.score) }),
      h("td", { text: s.regime }),
      h("td", { class: "muted", text: s.session }),
      h("td", {}, h("span", { class: `badge ${s.alert_status}`, text: s.alert_status === "suppressed" ? `held: ${s.suppressed_reason}` : s.alert_status })),
    )))));
}

function signalsView(app) {
  const box = h("div", {}, h("div", { class: "empty", text: "Loading…" }));
  app.append(h("div", { class: "stack" }, h("h2", { text: "All signals" }), box));
  api("/api/signals?limit=500").then((list) => box.replaceChildren(signalTable(list, "No signals yet."))).catch((e) => box.replaceChildren(errorBox(e)));
}
const errorBox = (e) => h("div", { class: "notice", text: String(e.message || e) });

// ---------------------------------------------------------------- chart
const LEVEL_LABEL = { swing_1h: "swing 1h", swing_4h: "swing 4h", high_7d: "7d high", high_30d: "30d high", hvn: "HVN" };
function makeChart(el, data, extra) {
  const LC = window.LightweightCharts;
  const p = data.precision;
  const minMove = p != null ? Math.pow(10, -p) : 0.0001;
  const chart = LC.createChart(el, {
    autoSize: true,
    layout: { background: { color: "#161a22" }, textColor: "#8a93a3", fontSize: 11, attributionLogo: true },
    grid: { vertLines: { color: "#1f242e" }, horzLines: { color: "#1f242e" } },
    rightPriceScale: { borderColor: "#29303c" },
    timeScale: { borderColor: "#29303c", timeVisible: true, secondsVisible: false,
      tickMarkFormatter: (t, type) => (type <= 2 ? dFmt : hmFmt).format(new Date(t * 1000)) },
    localization: { timeFormatter: (t) => dtFmt.format(new Date(t * 1000)), priceFormatter: (v) => price(v, p) },
    crosshair: { mode: 0 },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
  });
  const candles = chart.addSeries(LC.CandlestickSeries, {
    upColor: "#26a69a", downColor: "#ef5350", borderVisible: false, wickUpColor: "#26a69a", wickDownColor: "#ef5350",
    priceFormat: { type: "price", precision: Math.max(0, p ?? 4), minMove },
  });
  candles.setData(data.candles);
  const line = (d, color, style, title) => {
    const s = chart.addSeries(LC.LineSeries, { color, lineWidth: 1.5, lineStyle: style || 0, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false, title: "" });
    s.setData(d || []);
    return s;
  };
  line(data.ema20, "#f0b429", 0);
  line(data.ema50, "#b48cff", 0);
  line(data.vwap, "#5b9dff", 2);
  const pl = (priceV, color, title, style = 2, width = 1) => {
    if (isNum(priceV)) candles.createPriceLine({ price: priceV, color, lineWidth: width, lineStyle: style, axisLabelVisible: true, title });
  };
  const lastClose = data.candles.length ? data.candles[data.candles.length - 1].close : null;
  const levels = (extra.levels || data.levels || [])
    .filter((l) => lastClose == null || Math.abs(l.price / lastClose - 1) < 0.15)
    .sort((a, b) => Math.abs(a.price - lastClose) - Math.abs(b.price - lastClose)).slice(0, el.clientWidth < 600 ? 6 : 12);
  const merged = [];
  for (const l of levels.sort((a, b) => a.price - b.price)) {
    const m = merged.find((x) => Math.abs(x.price / l.price - 1) < 0.0005);
    const name = LEVEL_LABEL[l.kind] || l.kind;
    if (m) { if (!m.names.includes(name)) m.names.push(name); } else merged.push({ price: l.price, names: [name] });
  }
  for (const l of merged) pl(l.price, "rgba(138,147,163,0.55)", l.names.join(" · "), 1, 1);
  const box = extra.box !== undefined ? extra.box : data.watch;   // box: null = never draw
  if (box) { pl(box.box_high, "#4dd0e1", "coil high", 3, 1); pl(box.box_low, "#4dd0e1", "coil low", 3, 1); }
  for (const x of extra.lines || []) pl(x.price, x.color, x.title, x.style ?? 0, x.width ?? 1);
  if (extra.markerTime) {
    LC.createSeriesMarkers(candles, [{ time: extra.markerTime, position: "belowBar", color: "#f0b429", shape: "arrowUp", text: extra.markerText || "" }]);
  }
  chart.timeScale().scrollToRealTime();
  let last = data.candles.length ? { ...data.candles[data.candles.length - 1] } : null;
  return {
    chart,
    tick(px, tsMs) {
      if (!isNum(px) || !last) return;
      const bucket = Math.floor(tsMs / 900000) * 900;
      if (bucket < last.time) return;
      if (bucket > last.time) last = { time: bucket, open: last.close, high: px, low: px, close: px };
      else last = { ...last, high: Math.max(last.high, px), low: Math.min(last.low, px), close: px };
      candles.update(last);
    },
    destroy() { chart.remove(); },
  };
}
function legend(items) {
  return h("div", { class: "legend" }, items.map(([c, t, dashed]) => h("span", {}, h("i", { style: `background:${c};${dashed ? "opacity:.7" : ""}` }), t)));
}
const BASE_LEGEND = [["#f0b429", "EMA20 15m"], ["#b48cff", "EMA50 15m"], ["#5b9dff", "VWAP 24h", true], ["rgba(138,147,163,.8)", "resistance"]];

// ---------------------------------------------------------------- signal page
function signalView(app, id) {
  let chartObj = null;
  let sym = null;
  const load = api(`/api/signal/${id}`);
  app.append(h("div", { class: "empty", text: "Loading " + id + "…" }));
  load.then(async (sg) => {
    sym = sg.symbol;
    const p = sg.precision;
    const pl = sg.plan;
    app.replaceChildren();
    const status = sg.alert_status === "sent" ? h("span", { class: "badge sent", text: "sent to Telegram" })
      : sg.alert_status === "console" ? h("span", { class: "badge console", text: "console only (Telegram off)" })
      : sg.alert_status === "failed" ? h("span", { class: "badge suppressed", text: "Telegram send FAILED" })
      : h("span", { class: "badge suppressed", text: `not sent: ${sg.suppressed_reason || sg.alert_status}` });
    app.append(h("div", { class: "title-row" },
      h("span", { class: `badge ${sg.setup}`, text: sg.setup }),
      h("h1", { text: `${sg.symbol}` }),
      h("span", { class: "num", text: `score ${sg.score}` }),
      h("span", { class: "chip " + (sg.regime === "RISK_ON" ? "good" : sg.regime === "RISK_OFF" ? "bad" : "warn"), text: sg.regime }),
      h("span", { class: "muted", text: `#${sg.signal_id} · ${when(sg.bar_close_ms)} London` }), status));
    if (sg.tags && sg.tags.length) app.append(h("div", { class: "tags", style: "margin-bottom:12px" }, sg.tags.map((t) => h("span", { class: "chip muted", text: t }))));

    const chartEl = h("div", { class: "chart", role: "img", "aria-label": `${sg.symbol} 15 minute chart with plan levels` });
    const left = h("div", { class: "stack" },
      h("div", { class: "card" }, h("div", { class: "row spread" }, h("h2", { text: "15m chart (live)" }),
        h("a", { href: sg.tv_link || tvLink(sg.symbol), target: "_blank", rel: "noopener", text: "Open in TradingView ↗" })), chartEl,
        legend([...BASE_LEGEND, ["#e4e7ec", "entry"], ["#f0b429", "don't enter above"], ["#ef5350", "stop L"], ["#ff9f43", "stop S"], ["#8e2c2a", "liquidation"], ["#26a69a", "TP1 / TP2"], ["#4dd0e1", "coil box"]])),
      h("div", { class: "grid-3" }, condsCard("Conditions", sg.conds), breakdownCard(sg.breakdown), metricsCard(sg.features, sg.headroom)));
    const right = h("div", { class: "stack" }, planCard(pl, p), journalCard(sg));
    app.append(h("div", { class: "grid-2" }, left, right));

    const chartData = await api(`/api/chart/${sg.symbol}`).catch(() => null);
    if (!chartData || !chartData.candles.length) { chartEl.replaceChildren(h("div", { class: "empty", text: "Chart data unavailable." })); return; }
    chartData.precision = chartData.precision ?? p;
    const lines = [
      { price: pl.ref_entry, color: "#e4e7ec", title: "entry", style: 0 },
      { price: pl.chase_limit, color: "#f0b429", title: "max entry", style: 2 },
      { price: pl.stop_l.price, color: "#ef5350", title: "stop L", style: 0, width: 2 },
      { price: pl.stop_s.price, color: "#ff9f43", title: "stop S", style: 0, width: 2 },
      { price: pl.liq_price, color: "#8e2c2a", title: "liq", style: 0, width: 2 },
      { price: pl.tp1, color: "#26a69a", title: "TP1", style: 0 },
      { price: pl.tp2, color: "#26a69a", title: "TP2", style: 0 },
    ];
    const box = sg.setup === "COIL" && sg.extra && sg.extra.box_high ? { box_high: sg.extra.box_high, box_low: sg.extra.box_low } : null;
    const markerTime = Math.floor((sg.bar_close_ms - 1) / 900000) * 900;
    chartObj = makeChart(chartEl, chartData, { levels: sg.levels, lines, box, markerTime, markerText: sg.signal_id });
  }).catch((e) => app.replaceChildren(errorBox(e)));
  return {
    onPrices: (m) => { if (chartObj && sym && S.prices[sym]) chartObj.tick(S.prices[sym].last ?? S.prices[sym].mark, m.ts); },
    destroy: () => { if (chartObj) chartObj.destroy(); },
  };
}

function tvLink(sym) { return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent("WEEX:" + sym + ".P")}`; }

function planCard(pl, p) {
  const money = (x) => (isNum(x) ? (x < 0 ? "-$" : "+$") + Math.abs(x).toFixed(2) : "–");
  const mine = (S.data && S.data.header.default_stop_policy) || "L";
  const lbl = (s) => `Stop ${s.policy}` + (s.policy === mine ? " ◀ yours" : "");
  const stopRow = (s) => s.price == null
    ? h("tr", {}, h("td", { class: "left", text: lbl(s) }), h("td", { colspan: "5", class: "muted", text: `n/a — ${s.note}` }))
    : h("tr", {}, h("td", { class: "left", text: lbl(s) }), h("td", { class: "num", text: price(s.price, p) }),
      h("td", { class: "num neg", text: sgn(s.pct, 2, "%") }), h("td", { class: "num neg", text: money(s.loss_usd) }),
      h("td", { class: "num", text: fx(s.be_winrate * 100, 0) + "%" }), h("td", { class: "num", text: fx(s.win_r, 2) + "R" }));
  return h("div", { class: "card stack" },
    h("h2", { text: "Trade plan" }),
    h("dl", { class: "kv" },
      h("dt", { text: "Reference entry" }), h("dd", { text: price(pl.ref_entry, p) }),
      h("dt", { text: "Don't enter above" }), h("dd", { class: "warn-t", text: price(pl.chase_limit, p) }),
      h("dt", { text: `Liquidation (${pl.leverage}x, mark)` }), h("dd", { class: "neg", text: price(pl.liq_price, p) }),
      h("dt", { text: "Size" }), h("dd", { text: `$${fx(pl.margin, 0)} × ${pl.leverage}x = $${fx(pl.notional, 0)}` }),
      h("dt", { text: "Maint. margin rate" }), h("dd", { text: fx(pl.mmr * 100, 2) + "%" })),
    h("div", { class: "table-wrap" }, h("table", { class: "plan-table" },
      h("thead", {}, h("tr", {}, ["", "Price", "Move", "$ P&L", "BE win", "All-TP R"].map((t, i) => h("th", { class: i ? "" : "left", text: t })))),
      h("tbody", {}, ...(mine === "S" ? [stopRow(pl.stop_s), stopRow(pl.stop_l)] : [stopRow(pl.stop_l), stopRow(pl.stop_s)]),
        h("tr", {}, h("td", { class: "left", text: `TP1 (${pl.tp1_close_pct}%)` }), h("td", { class: "num", text: price(pl.tp1, p) }),
          h("td", { class: "num pos", text: sgn(pl.tp1_pct, 1, "%") }), h("td", { class: "num pos", text: money(pl.tp1_leg_usd) }), h("td", {}), h("td", {})),
        pl.tp2 != null
          ? h("tr", {}, h("td", { class: "left", text: `TP2 (${pl.tp2_close_pct}%)` }), h("td", { class: "num", text: price(pl.tp2, p) }),
            h("td", { class: "num pos", text: sgn(pl.tp2_pct, 1, "%") }), h("td", { class: "num pos", text: money(pl.tp2_leg_usd) }), h("td", {}), h("td", {}))
          : h("tr", {}, h("td", { class: "left", text: "TP2" }), h("td", { colspan: "5", class: "muted", text: "dropped (resistance too close)" }))))),
    h("p", { class: "small muted", style: "margin:0" },
      `Runner ${pl.runner_pct}%: chandelier (2.5×ATR15m) / 15m swing-low trail, exit on 15m close below EMA20; stop only moves up. `,
      `All targets hit: ${money(pl.win_usd)} (fees + slippage included). BE win = win rate needed to break even with that stop.`),
    ...(pl.warnings || []).map((w) => h("div", { class: "notice", text: w })));
}

function journalCard(sg) { return takeTradeCard(sg); }   // defined in journal.js

function condsCard(title, conds, disabledNote) {
  if (!conds || !conds.length) return h("div", { class: "card" }, h("h3", { text: title }), h("div", { class: "muted small", text: disabledNote || "Not evaluated this bar." }));
  return h("div", { class: "card" }, h("h3", { text: title }),
    h("ul", { class: "conds" }, conds.map((c) => {
      const st = c.passed === true ? "ok" : c.passed === false ? "no" : "na";
      if (c.off) return h("li", { class: "off", title: "Switched off in config.yaml (signals.disabled_conditions): not required" },
        h("span", {}, h("span", { class: "mark", text: "·" }), c.name + " (off)"), h("span", { class: "num muted", text: c.value }));
      return h("li", { class: st }, h("span", {}, h("span", { class: "mark", text: st === "ok" ? "✓" : st === "no" ? "✗" : "–" }), c.name),
        h("span", { class: "num muted", text: c.value }));
    })));
}

function breakdownCard(bd) {
  const LABEL = { rvol: "RVOL", rs: "RS", oi: "OI", flow: "Flow", headroom: "Room", regime: "Regime", funding: "Funding", wick: "Wicks" };
  const MAX = 25;
  return h("div", { class: "card" }, h("h3", { text: "Score breakdown" }),
    h("div", { class: "bars" }, Object.entries(bd || {}).map(([k, v]) => h("div", { class: "bar" },
      h("span", { class: "muted small", text: LABEL[k] || k }),
      h("div", { class: "track" }, h("div", { class: "fill" + (v < 0 ? " neg" : ""), style: `width:${Math.min(100, Math.abs(v) / MAX * 100)}%` })),
      h("span", { class: "num small", text: (v > 0 ? "+" : "") + v })))));
}

function metricsCard(f, hr) {
  const rows = [
    ["Price", price(f.price)], ["Return 1h / 4h / 24h", `${sgn(f.ret_1h)} / ${sgn(f.ret_4h)} / ${sgn(f.ret_24h, 1)}%`],
    ["RS vs BTC 1h", sgn(f.rs_1h, 2, "%")], ["RVOL 5m / 15m", `${fx(f.rvol_5m, 1)}x / ${fx(f.rvol_15m, 1)}x`],
    ["OI Δ 15m / 1h / 4h", `${sgn(f.oi_chg_15m, 1)} / ${sgn(f.oi_chg_1h, 1)} / ${sgn(f.oi_chg_4h, 1)}%`],
    ["Funding (8h eq.)", isNum(f.funding_8h) ? sgn(f.funding_8h * 100, 4, "%") : "–"],
    ["Taker buy 15m", fx(f.taker_buy_ratio_15m, 2)], ["CVD slope 1h", sgn(f.cvd_slope_1h_norm, 2)],
    ["ATR 1h", fx(f.atr_1h_pct, 2) + "%"], ["BBW pct (30d)", fx(f.bbw_pct_1h, 0)],
    ["Deep wicks 24h", String(f.deep_wicks_24h ?? "–")],
  ];
  if (hr) rows.push(["Headroom", `${fx(hr.pct, 1)}%${hr.price_discovery ? " (discovery)" : hr.kind ? " to " + (LEVEL_LABEL[hr.kind] || hr.kind) : ""}`]);
  return h("div", { class: "card" }, h("h3", { text: "Key metrics" }),
    h("dl", { class: "kv small" }, rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])));
}

// ---------------------------------------------------------------- symbol page
function symbolView(app, sym) {
  let chartObj = null;
  let lastBar = null;
  const head = h("div", { class: "title-row" }, h("h1", { text: sym }));
  const tvA = h("a", { href: tvLink(sym), target: "_blank", rel: "noopener", text: "Open in TradingView ↗" });
  const chartEl = h("div", { class: "chart", role: "img", "aria-label": `${sym} 15 minute chart` });
  const panels = h("div", { class: "grid-3" });
  const side = h("div", { class: "stack" });
  app.append(head, h("div", { class: "grid-2" },
    h("div", { class: "stack" }, h("div", { class: "card" }, h("div", { class: "row spread" }, h("h2", { text: "15m chart (live)" }),
      tvA), chartEl, legend([...BASE_LEGEND, ["#4dd0e1", "coil box"]])), panels),
    side));

  async function loadDetail() {
    try {
      const d = await api(`/api/symbol/${sym}`);
      head.replaceChildren(h("h1", { text: sym }), h("span", { class: `badge ${d.state}`, text: d.state }),
        h("span", { class: "num", text: `score ${d.score} (${d.setup.toLowerCase()})` }),
        h("span", { class: "muted", text: `${d.n_pass}/${d.n_conds} conditions` }));
      panels.replaceChildren(condsCard("Ignition", d.ignition_conds), condsCard("Coil — WATCH", d.watch_conds),
        condsCard("Coil — ENTRY", d.coil_entry_conds, d.watch ? "Checked at each 15m close while watching." : "Only checked while in WATCH."));
      side.replaceChildren(breakdownCard(d.breakdown), metricsCard(d.features, { pct: d.headroom_pct, price_discovery: d.price_discovery }),
        d.watch ? h("div", { class: "card" }, h("h3", { text: "Coil box" }), h("dl", { class: "kv" },
          h("dt", { text: "High" }), h("dd", { text: price(d.watch.box_high, d.precision) }),
          h("dt", { text: "Low" }), h("dd", { text: price(d.watch.box_low, d.precision) }),
          h("dt", { text: "Watching since" }), h("dd", { text: when(d.watch.since_ms) }))) : null);
    } catch (e) {
      side.replaceChildren(h("div", { class: "notice", text: `${e.message}. The chart still works; conditions show for symbols in the current universe.` }));
    }
  }
  async function loadChart() {
    try {
      const c = await api(`/api/chart/${sym}`);
      if (c.tv_link) tvA.href = c.tv_link;
      if (!c.candles.length) return;
      const lb = c.candles[c.candles.length - 1].time;
      if (lb === lastBar && chartObj) return;
      lastBar = lb;
      if (chartObj) chartObj.destroy();
      chartEl.replaceChildren();
      chartObj = makeChart(chartEl, c, {});
    } catch (e) { chartEl.replaceChildren(errorBox(e)); }
  }
  loadDetail(); loadChart();
  return {
    onState: () => { loadDetail(); loadChart(); },
    onPrices: (m) => { const p = S.prices[sym]; if (chartObj && p) chartObj.tick(p.last ?? p.mark, m.ts); },
    destroy: () => { if (chartObj) chartObj.destroy(); },
  };
}

// ---------------------------------------------------------------- settings
function settingsView(app) {
  app.append(h("div", { class: "empty", text: "Loading settings…" }));
  api("/api/settings").then((st) => {
    app.replaceChildren();
    const v = st.values;
    const inp = (id, val, attrs) => h("input", { id, value: String(val), inputmode: "decimal", ...attrs });
    const margin = inp("margin", v.margin_usd), lev = inp("leverage", v.leverage), notional = inp("notional", v.notional_usd);
    const maker = inp("maker", v.maker_fee), taker = inp("taker", v.taker_fee);
    const bed = h("input", { id: "bed", type: "time", value: v.bedtime_reminder });
    let edited = "margin";
    let refreshInfo = () => {};
    const link3 = (src) => {
      edited = src;
      const m = parseFloat(margin.value), l = parseFloat(lev.value), n = parseFloat(notional.value);
      if (!(l > 0)) return;
      if (src === "notional") { if (isFinite(n)) margin.value = +(n / l).toFixed(4); }
      else if (isFinite(m)) notional.value = +(m * l).toFixed(4);
    };
    margin.addEventListener("input", () => link3("margin"));
    lev.addEventListener("input", () => link3("leverage"));
    notional.addEventListener("input", () => link3("notional"));
    const feePct = (el) => { const s = h("span", { class: "hint" }); const upd = () => { const f = parseFloat(el.value); s.textContent = isFinite(f) ? `= ${(f * 100).toFixed(3)}%` : ""; }; el.addEventListener("input", upd); upd(); return s; };
    const field = (label, el, hint) => h("div", { class: "field" }, h("label", { for: el.id, text: label }), el, hint ? (typeof hint === "string" ? h("span", { class: "hint", text: hint }) : hint) : null);
    const msg = h("div", { class: "msg" });
    const save = h("button", { class: "btn primary", type: "submit", text: "Save" });
    const form = h("form", { class: "card stack", onsubmit: async (e) => {
      e.preventDefault(); save.disabled = true; msg.className = "msg"; msg.textContent = "Saving…";
      try {
        const r = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
          edited, values: { margin_usd: margin.value, leverage: lev.value, notional_usd: notional.value, maker_fee: maker.value, taker_fee: taker.value, bedtime_reminder: bed.value } }) });
        margin.value = r.values.margin_usd; lev.value = r.values.leverage; notional.value = r.values.notional_usd;
        const n = Object.keys(r.changes).length;
        msg.className = "msg ok"; msg.textContent = n ? `Saved ${n} change(s) to config.yaml (config ${r.config_hash}). New signals use them immediately.` : "No changes.";
        refreshInfo();
      } catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
      save.disabled = false;
    } },
      h("h2", { text: "Trade defaults" }),
      h("p", { class: "muted small", style: "margin:0", text: "Used for every new signal's plan and as the pre-filled values in the trade journal. Margin × leverage = notional: editing margin or leverage recalculates notional; editing notional recalculates margin at the current leverage." }),
      h("div", { class: "form-grid" },
        field("Margin (USDT)", margin), field("Leverage (x)", lev, "Per-coin maximums are checked on each plan"), field("Notional (USDT)", notional),
        field("Maker fee (fraction)", maker, feePct(maker)), field("Taker fee (fraction)", taker, feePct(taker)),
        field("Bedtime reminder (London)", bed, "Used by the trade journal (M4)")),
      h("div", { class: "row" }, save, msg));
    const versions = h("div", { class: "card" });
    const ro = h("div", { class: "card stack" });
    const drawInfo = (st) => {
      versions.replaceChildren(h("h2", { text: "Config history" }), st.versions.length ? h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["When (London)", "Config", "Source", "Changes"].map((t) => h("th", { class: "left", text: t })))),
        h("tbody", {}, st.versions.map((x) => h("tr", {}, h("td", { class: "left num", text: when(x.ts) }), h("td", { class: "left num", text: x.hash }),
          h("td", { class: "left", text: x.source }), h("td", { class: "left small", text: Object.entries(x.changes || {}).map(([k, [a, b]]) => `${k}: ${a} → ${b}`).join("; ") || "–" })))))) : h("div", { class: "muted", text: "No history yet." }));
      ro.replaceChildren(h("div", { class: "row spread" }, h("h2", { text: "Full configuration (read-only)" }), h("span", { class: "muted small num", text: `config ${st.config_hash}` })),
        h("p", { class: "muted small", style: "margin:0", text: "Everything else is edited in config.yaml (restart the app afterwards). Secrets live in .env and are never shown here." }),
        h("pre", { class: "config", text: st.config_yaml }));
    };
    drawInfo(st);
    refreshInfo = () => api("/api/settings").then(drawInfo).catch(() => {});
    app.append(h("div", { class: "stack" }, form, versions, ro));
  }).catch((e) => app.replaceChildren(errorBox(e)));
}

// ---------------------------------------------------------------- boot (called from journal.js, loaded last)
function boot() {
  renderHeader(true);
  connect();
  navigate(location.pathname, false);
}
