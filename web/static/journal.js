/* Trade journal, paper trades and stats pages (M4). Uses helpers from app.js. */
"use strict";

// ---------------------------------------------------------------- shared helpers
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const money = (x) => (isNum(x) ? (x < 0 ? "-$" : "+$") + Math.abs(x).toFixed(2) : "–");
const moneyCls = (x) => (isNum(x) ? (x > 0 ? "pos" : x < 0 ? "neg" : "") : "faint");
const rFmt = (x) => (isNum(x) ? (x > 0 ? "+" : "") + x.toFixed(2) + "R" : "–");
const pct0 = (x) => (isNum(x) ? (x * 100).toFixed(0) + "%" : "–");
const londonParts = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
function londonInput(ms) {   // value for <input type=datetime-local>, in London time
  const p = Object.fromEntries(londonParts.formatToParts(new Date(ms ?? Date.now())).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}
function nowPrice(sym) { const p = S.prices[sym]; return p ? (p.last ?? p.mark) : null; }
function statusBadge(st) {
  const c = st === "OPEN" ? "WATCH" : st === "CLOSED" ? "SKIP" : "failed";
  return h("span", { class: `badge ${c}`, text: st === "NEEDS_CONFIRMATION" ? "NEEDS CONFIRMATION" : st });
}

// A small modal form. fields: [{name, label, type, value, step, options, hint}]
function dialogForm(title, fields, submitText, onSubmit, intro) {
  const dlg = h("dialog", { class: "dlg" });
  const msg = h("div", { class: "msg" });
  const inputs = {};
  const form = h("form", { method: "dialog", class: "stack" },
    h("h2", { text: title }), intro ? h("p", { class: "muted small", text: intro }) : null,
    h("div", { class: "form-grid" }, fields.map((f) => {
      let el;
      if (f.options) el = h("select", { id: "f_" + f.name }, f.options.map(([v, t]) => h("option", { value: v, selected: String(v) === String(f.value) }, t)));
      else el = h("input", { id: "f_" + f.name, type: f.type || "text", value: f.value ?? "", step: f.step, inputmode: f.type === "number" ? "decimal" : null });
      inputs[f.name] = el;
      return h("div", { class: "field" }, h("label", { for: el.id, text: f.label }), el, f.hint ? h("span", { class: "hint", text: f.hint }) : null);
    })),
    h("div", { class: "row" },
      h("button", { class: "btn primary", type: "submit", text: submitText }),
      h("button", { class: "btn", type: "button", text: "Cancel", onclick: () => dlg.close() }), msg));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const vals = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
    msg.className = "msg"; msg.textContent = "Saving…";
    try { await onSubmit(vals); dlg.close(); }
    catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
  });
  dlg.append(form);
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}

// ---------------------------------------------------------------- trade actions (buttons)
function tradeButtons(t, after) {
  const p = t.precision;
  const px = () => { const v = nowPrice(t.symbol) ?? t.price; return isNum(v) ? +v.toFixed(Math.max(p ?? 6, 0)) : ""; };
  const done = async (r) => { if (r && r.warning) alert("Saved, with a warning:\n" + r.warning); after && after(); };
  const b = (text, cls, fn) => h("button", { class: "btn " + (cls || ""), type: "button", text, onclick: fn });
  const out = [];
  if (t.status !== "CLOSED") {
    out.push(b("Close", "primary", () => dialogForm(`Close ${t.symbol} (${t.trade_ref})`, [
      { name: "price", label: "Exit price", type: "number", step: "any", value: px() },
      { name: "time", label: "Time (London)", type: "datetime-local", value: londonInput() },
      { name: "order", label: "Order type", value: "market", options: [["market", "market (taker)"], ["limit", "limit (maker)"]] },
    ], "Log close", async (v) => done(await post(`/api/trade/${t.trade_ref}/close`, v)),
    "Records the exit you made on WEEX. It does not touch WEEX.")));
    out.push(b("Partial close", "", () => dialogForm(`Partial close ${t.symbol}`, [
      { name: "price", label: "Price", type: "number", step: "any", value: px() },
      { name: "pct", label: "% of original position", type: "number", step: "any", value: 40, hint: "or fill in quantity instead" },
      { name: "qty", label: "Quantity (optional)", type: "number", step: "any", value: "" },
      { name: "time", label: "Time (London)", type: "datetime-local", value: londonInput() },
      { name: "order", label: "Order type", value: "limit", options: [["limit", "limit (maker)"], ["market", "market (taker)"]] },
    ], "Log partial close", async (v) => done(await post(`/api/trade/${t.trade_ref}/partial`, v)))));
    out.push(b("Moved stop", "", () => dialogForm(`Moved stop ${t.symbol}`, [
      { name: "price", label: "New stop price", type: "number", step: "any", value: t.suggested_stop != null ? +t.suggested_stop.toFixed(Math.max(p ?? 6, 0)) : t.current_stop },
      { name: "time", label: "Time (London)", type: "datetime-local", value: londonInput() },
    ], "Log stop move", async (v) => done(await post(`/api/trade/${t.trade_ref}/stop`, v)),
    t.suggested_stop != null ? `Suggested by the plan: ${price(t.suggested_stop, p)}` : null)));
    if (t.status === "NEEDS_CONFIRMATION") {
      out.push(b("Still open", "", () => dialogForm(`Confirm ${t.trade_ref} is still open`, [
        { name: "note", label: "Note (e.g. I had moved my SL)", value: "" },
      ], "Confirm still open", async (v) => done(await post(`/api/trade/${t.trade_ref}/confirm`, v)),
      "Use this only if the position is really still open on WEEX. Otherwise log the close.")));
    }
  }
  out.push(b("Edit", "", () => dialogForm(`Edit ${t.trade_ref}`, [
    { name: "entry_price", label: "Entry price", type: "number", step: "any", value: t.entry_price },
    { name: "entry_time", label: "Entry time (London)", type: "datetime-local", value: londonInput(t.entry_ms) },
    { name: "margin", label: "Margin", type: "number", step: "any", value: t.margin },
    { name: "leverage", label: "Leverage", type: "number", step: "any", value: t.leverage },
    { name: "initial_stop", label: "Initial stop", type: "number", step: "any", value: t.initial_stop },
    { name: "tp1", label: "TP1", type: "number", step: "any", value: t.tp1 ?? "" },
    { name: "tp2", label: "TP2", type: "number", step: "any", value: t.tp2 ?? "" },
    { name: "notes", label: "Notes", value: t.notes || "" },
  ], "Save edit", async (v) => done(await post(`/api/trade/${t.trade_ref}/edit`, { ...v, edited: "margin" })),
  "Every edit is kept in the audit trail (old → new). Changing entry/size/stop/targets re-runs the plan.")));
  return h("div", { class: "row" }, out);
}

// ---------------------------------------------------------------- "I took this" (signal page) + own trade form
function takeTradeCard(sg) {
  const card = h("div", { class: "card stack" });
  card.append(h("h2", { text: "I took this" }), tradeForm({ signal: sg }), paperCard(sg.signal_id));
  return card;
}

function tradeForm({ signal }) {
  const sym = signal ? signal.symbol : null;
  const wrap = h("form", { class: "stack", autocomplete: "off" });
  const inp = (id, val, attrs = {}) => h("input", { id, value: val ?? "", inputmode: "decimal", ...attrs });
  const symIn = h("input", { id: "tf_sym", value: sym || "", list: "tf_syms", placeholder: "e.g. SOLUSDT", disabled: !!sym, autocapitalize: "characters" });
  const dl = h("datalist", { id: "tf_syms" });
  if (!sym) api("/api/symbols").then((l) => dl.replaceChildren(...l.map((s) => h("option", { value: s })))).catch(() => {});
  const startPx = sym ? (nowPrice(sym) ?? signal.plan.ref_entry) : "";
  const entry = inp("tf_entry", isNum(startPx) ? startPx : "", { type: "number", step: "any" });
  const time = h("input", { id: "tf_time", type: "datetime-local", value: londonInput() });
  const margin = inp("tf_margin", signal ? signal.plan.margin : ""), lev = inp("tf_lev", signal ? signal.plan.leverage : ""), notional = inp("tf_not", signal ? signal.plan.notional : "");
  const myPol = (S.data && S.data.header.default_stop_policy) || "L";
  const pol = h("select", { id: "tf_pol" }, [["L", "Policy L (liquidation-buffered)"], ["S", "Policy S (structural)"], ["custom", "Custom"]]
    .map(([v, t]) => h("option", { value: v, selected: v === myPol }, t + (v === myPol ? " - your model" : ""))));
  const stop = inp("tf_stop", "", { type: "number", step: "any" });
  const tp1 = inp("tf_tp1", "", { type: "number", step: "any" }), tp2 = inp("tf_tp2", "", { type: "number", step: "any" });
  const order = h("select", { id: "tf_order" }, [["market", "market (taker)"], ["limit", "limit (maker)"]].map(([v, t]) => h("option", { value: v }, t)));
  const notes = h("input", { id: "tf_notes", placeholder: "optional" });
  const info = h("div", { class: "small stack" });
  const msg = h("div", { class: "msg" });
  let edited = "margin", userStop = false, userTp = false, timer = null;

  if (!signal) {   // own trade: defaults from Settings
    api("/api/settings").then((st) => { margin.value = st.values.margin_usd; lev.value = st.values.leverage; notional.value = st.values.notional_usd; schedule(); }).catch(() => {});
  }
  const link3 = (src) => {
    edited = src;
    const m = parseFloat(margin.value), l = parseFloat(lev.value), n = parseFloat(notional.value);
    if (!(l > 0)) return;
    if (src === "notional") { if (isFinite(n)) margin.value = +(n / l).toFixed(4); } else if (isFinite(m)) notional.value = +(m * l).toFixed(4);
  };
  margin.addEventListener("input", () => { link3("margin"); schedule(); });
  lev.addEventListener("input", () => { link3("leverage"); schedule(); });
  notional.addEventListener("input", () => { link3("notional"); schedule(); });
  entry.addEventListener("input", schedule);
  symIn.addEventListener("change", schedule);
  pol.addEventListener("change", () => { userStop = pol.value === "custom"; schedule(); });
  stop.addEventListener("input", () => { userStop = true; pol.value = "custom"; schedule(); });
  tp1.addEventListener("input", () => { userTp = true; });
  tp2.addEventListener("input", () => { userTp = true; });

  function body() {
    return { signal_id: signal ? signal.signal_id : null, symbol: signal ? sym : symIn.value.trim().toUpperCase(),
      entry_price: entry.value, entry_time: time.value, margin: margin.value, leverage: lev.value, notional: notional.value,
      edited, stop_policy: pol.value, stop_price: pol.value === "custom" ? stop.value : "", tp1: tp1.value, tp2: tp2.value,
      entry_order: order.value, notes: notes.value };
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(preview, 250); }
  async function preview() {
    const b = body();
    if (!b.symbol || !(parseFloat(b.entry_price) > 0) || !(parseFloat(b.margin) > 0) || !(parseFloat(b.leverage) > 0)) return;
    try {
      const r = await post("/api/trades/preview", b);
      const p = sg_p(r);
      if (!userStop) stop.value = r.stop ?? "";
      if (!userTp) { tp1.value = r.tp1 ?? ""; tp2.value = r.tp2 ?? ""; }
      info.replaceChildren(
        h("dl", { class: "kv" },
          h("dt", { text: `Est. liquidation (${r.leverage}x, mark)` }), h("dd", { class: "neg", text: price(r.liq_price, p) }),
          h("dt", { text: "Stop L (liq-buffered)" }), h("dd", { text: price(r.stop_l, p) }),
          h("dt", { text: "Stop S (structural)" }), h("dd", { text: r.stop_s != null ? price(r.stop_s, p) : "n/a – " + r.stop_s_note }),
          h("dt", { text: "Size" }), h("dd", { text: `$${fx(r.margin, 2)} × ${r.leverage}x = $${fx(r.notional, 2)}` }),
          r.entry_gap_pct != null ? [h("dt", { text: "Entry vs signal" }), h("dd", { text: sgn(r.entry_gap_pct, 2, "%") })] : null),
        ...(r.warnings || []).map((w) => h("div", { class: "notice", text: w })));
    } catch (e) { info.replaceChildren(h("div", { class: "notice", text: e.message })); }
  }
  const sg_p = (r) => (signal ? signal.precision : null);
  const field = (label, el, hint) => h("div", { class: "field" }, h("label", { for: el.id, text: label }), el, hint ? h("span", { class: "hint", text: hint }) : null);
  wrap.append(
    h("p", { class: "muted small", style: "margin:0", text: "Records the trade you placed on WEEX (this app never trades). Prices and times are pre-filled; change them to what you actually got. Times are London time; you can log hours afterwards and the plan is replayed from your entry time." }),
    h("div", { class: "form-grid" },
      field("Symbol", symIn), dl, field("Entry price", entry), field("Entry time (London)", time),
      field("Margin (USDT)", margin), field("Leverage (x)", lev), field("Notional (USDT)", notional, "margin × leverage"),
      field("Stop policy", pol), field("Stop price", stop, "recalculated from your entry and leverage"),
      field("TP1", tp1), field("TP2", tp2, "blank = dropped"), field("Entry order", order), field("Notes", notes)),
    info,
    h("div", { class: "row" }, h("button", { class: "btn primary", type: "submit", text: "Save trade" }), msg));
  wrap.addEventListener("submit", async (e) => {
    e.preventDefault();
    msg.className = "msg"; msg.textContent = "Saving…";
    try { const t = await post("/api/trades", body()); navigate(`/trade/${t.trade_ref}`); }
    catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
  });
  setTimeout(preview, 50);
  return wrap;
}

function paperCard(signalId) {
  const box = h("div", {}, h("h3", { text: "Baseline paper trades" }), h("div", { class: "muted small", text: "Loading…" }));
  api(`/api/paper?signal_id=${encodeURIComponent(signalId)}`).then((rows) => {
    box.replaceChildren(h("h3", { text: "Baseline paper trades" }), rows.length ? paperTable(rows, false) : h("div", { class: "muted small", text: "None." }));
  }).catch(() => {});
  return box;
}

function paperTable(rows, withSignal = true) {
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, [...(withSignal ? ["Signal", "Symbol"] : []), "Policy", "Status", "Entry (London)", "Exit", "Path", "Net", "R", "MFE/MAE"].map((t, i) => h("th", { class: i < (withSignal ? 2 : 0) ? "left" : "", text: t })))),
    h("tbody", {}, rows.map((r) => h("tr", {},
      ...(withSignal ? [h("td", { class: "left" }, link(`/signal/${r.signal_id}`, r.signal_id)), h("td", { class: "left", text: r.symbol })] : []),
      h("td", { text: r.policy }), h("td", {}, h("span", { class: `badge ${r.status === "OPEN" ? "WATCH" : "SKIP"}`, text: r.status })),
      h("td", { class: "num", text: when(r.entry_ms) }),
      h("td", { class: "small", text: r.exit_reason || (r.current_stop != null ? `stop ${price(r.current_stop)}` : "") }),
      h("td", { class: "small muted", text: (r.legs || []).map((l) => l.reason).join("+") || "–" }),
      h("td", { class: "num " + moneyCls(r.net_pnl), text: money(r.net_pnl) }),
      h("td", { class: "num", text: rFmt(r.r_multiple) }),
      h("td", { class: "num small muted", text: `${sgn(r.mfe_pct, 1)} / ${sgn(r.mae_pct, 1)}%` }))))));
}

// ---------------------------------------------------------------- My trades
function tradesView(app) {
  const top = h("div", { class: "row spread" }, h("h2", { text: "My trades", style: "margin:0" }),
    h("div", { class: "row" }, h("a", { href: "/trades/new", "data-link": true, class: "btn", text: "+ Add own trade" }),
      h("a", { href: "/trades/close-all", "data-link": true, class: "btn danger", text: "Close all open trades" })));
  const needsBox = h("div"), openBox = h("div", { class: "stack" }), closedBox = h("div");
  app.append(h("div", { class: "stack" }, top, needsBox,
    h("div", { class: "card stack" }, h("h2", { text: "Open" }), openBox),
    h("div", { class: "card" }, h("h2", { text: "Closed" }), closedBox)));
  let timer = null;
  async function load() {
    try {
      const d = await api("/api/trades");
      const needs = d.open.filter((t) => t.status === "NEEDS_CONFIRMATION");
      needsBox.replaceChildren(needs.length ? h("div", { class: "card stack needs" }, h("h2", { text: `Needs confirmation (${needs.length})` }),
        h("p", { class: "small", style: "margin:0", text: "Price crossed your stop or a plan exit while you hadn't logged anything. Check WEEX, then log the close (or confirm it's still open)." }),
        needs.map((t) => tradeCard(t, load))) : "");
      const open = d.open.filter((t) => t.status !== "NEEDS_CONFIRMATION");
      openBox.replaceChildren(...(open.length ? open.map((t) => tradeCard(t, load)) : [h("div", { class: "muted", text: "No open trades. Log one from a signal page (\"I took this\") or with + Add own trade." })]));
      closedBox.replaceChildren(closedTable(d.closed));
    } catch (e) { openBox.replaceChildren(errorBox(e)); }
  }
  load();
  timer = setInterval(load, 5000);
  return { onState: load, destroy: () => clearInterval(timer) };
}

function tradeCard(t, after) {
  const p = t.precision;
  return h("div", { class: "trade-card" + (t.status === "NEEDS_CONFIRMATION" ? " needs" : "") },
    h("div", { class: "row spread" },
      h("div", { class: "row" }, h("strong", {}, link(`/trade/${t.trade_ref}`, `${t.symbol}`)), statusBadge(t.status),
        h("span", { class: "muted small" }, t.signal_id ? link(`/signal/${t.signal_id}`, t.signal_id) : "own trade", ` · ${t.trade_ref} · ${when(t.entry_ms)}`)),
      h("div", { class: "num big " + moneyCls(t.mtm_pnl), title: "Mark-to-market, unconfirmed", text: money(t.mtm_pnl) })),
    h("dl", { class: "kv small cols" },
      h("dt", { text: "Entry" }), h("dd", { text: price(t.entry_price, p) }),
      h("dt", { text: "Now" }), h("dd", { text: price(nowPrice(t.symbol) ?? t.price, p) }),
      h("dt", { text: "Size" }), h("dd", { text: `$${fx(t.margin, 0)} × ${t.leverage}x · open ${fx(t.qty_open / t.qty * 100, 0)}%` }),
      h("dt", { text: "Your SL" }), h("dd", { text: price(t.current_stop, p) }),
      h("dt", { text: "Suggested SL" }), h("dd", { class: t.suggested_stop > t.current_stop ? "warn-t" : "", text: t.suggested_stop != null ? price(t.suggested_stop, p) : "–" }),
      h("dt", { text: "Liq (est.)" }), h("dd", { class: "neg", text: price(t.liq_price, p) })),
    h("div", { class: "next" }, h("span", { class: "muted small", text: "Next: " }), t.next_action),
    tradeButtons(t, after));
}

function closedTable(rows) {
  if (!rows.length) return h("div", { class: "muted", text: "No closed trades yet." });
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["Trade", "Symbol", "Source", "Entry (London)", "Closed", "Net", "R", "vs plan", "Entry gap"].map((t, i) => h("th", { class: i < 3 ? "left" : "", text: t })))),
    h("tbody", {}, rows.map((t) => h("tr", { class: "clickable", onclick: (e) => { if (!e.target.closest("a")) navigate(`/trade/${t.trade_ref}`); } },
      h("td", { class: "left" }, link(`/trade/${t.trade_ref}`, t.trade_ref)), h("td", { class: "left", text: t.symbol }),
      h("td", { class: "left" }, t.signal_id ? link(`/signal/${t.signal_id}`, t.signal_id) : "own"),
      h("td", { class: "num", text: when(t.entry_ms) }), h("td", { class: "num", text: when(t.closed_ms) }),
      h("td", { class: "num " + moneyCls(t.net_pnl), text: money(t.net_pnl) }), h("td", { class: "num", text: rFmt(t.r_multiple) }),
      h("td", { class: "num " + moneyCls(t.adherence_usd), title: "Your net minus what the exit engine would have made on your entry", text: money(t.adherence_usd) }),
      h("td", { class: "num", text: t.entry_gap_pct != null ? sgn(t.entry_gap_pct, 2, "%") : "–" }))))));
}

// ---------------------------------------------------------------- Close all
function closeAllView(app) {
  app.append(h("div", { class: "empty", text: "Loading open trades…" }));
  api("/api/trades").then((d) => {
    app.replaceChildren();
    if (!d.open.length) { app.append(h("div", { class: "card" }, h("h2", { text: "Close all" }), h("p", { class: "muted", text: "No open logged trades." }), link("/trades", "← My trades"))); return; }
    const time = h("input", { type: "datetime-local", id: "ca_time", value: londonInput() });
    const rows = d.open.map((t) => {
      const px = nowPrice(t.symbol) ?? t.price;
      const input = h("input", { type: "number", step: "any", value: isNum(px) ? +px.toFixed(Math.max(t.precision ?? 6, 0)) : "", inputmode: "decimal", "aria-label": `${t.symbol} exit price` });
      return { t, input };
    });
    const msg = h("div", { class: "msg" });
    const btn = h("button", { class: "btn danger big-btn", type: "button", text: `Confirm: close all ${rows.length} trade${rows.length > 1 ? "s" : ""}` });
    btn.addEventListener("click", async () => {
      btn.disabled = true; msg.className = "msg"; msg.textContent = "Logging…";
      try {
        const r = await post("/api/trades/close_all", { items: rows.map(({ t, input }) => ({ trade_ref: t.trade_ref, price: input.value, time: time.value })) });
        msg.className = r.errors.length ? "msg err" : "msg ok";
        msg.textContent = `Logged ${r.closed.length} close(s).` + (r.errors.length ? " Problems: " + r.errors.join("; ") : "");
        if (!r.errors.length) setTimeout(() => navigate("/trades"), 900);
      } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
      btn.disabled = false;
    });
    app.append(h("div", { class: "card stack" },
      h("h2", { text: "Close all open trades" }),
      h("p", { class: "muted small", style: "margin:0", text: "First close everything on WEEX. Then check each exit price below (pre-filled with the current price), adjust to your actual fills, and confirm once. This only records - it never touches WEEX." }),
      h("div", { class: "field", style: "max-width:260px" }, h("label", { for: "ca_time", text: "Close time (London)" }), time),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Trade", "Symbol", "Entry", "Open", "Unconfirmed P&L", "Exit price"].map((x, i) => h("th", { class: i < 2 ? "left" : "", text: x })))),
        h("tbody", {}, rows.map(({ t, input }) => h("tr", {},
          h("td", { class: "left" }, link(`/trade/${t.trade_ref}`, t.trade_ref)), h("td", { class: "left", text: t.symbol }),
          h("td", { class: "num", text: price(t.entry_price, t.precision) }), h("td", { class: "num", text: fx(t.qty_open / t.qty * 100, 0) + "%" }),
          h("td", { class: "num " + moneyCls(t.mtm_pnl), text: money(t.mtm_pnl) }), h("td", {}, input)))))),
      h("div", { class: "row" }, btn, msg)));
  }).catch((e) => app.replaceChildren(errorBox(e)));
}

// ---------------------------------------------------------------- Trade detail
function tradeView(app, ref) {
  let chartObj = null, sym = null, timer = null;
  const head = h("div", { class: "title-row" }, h("h1", { text: ref }));
  const body = h("div", { class: "grid-2" });
  app.append(head, body);
  async function load(first) {
    let t;
    try { t = await api(`/api/trade/${ref}`); } catch (e) { body.replaceChildren(errorBox(e)); return; }
    sym = t.symbol;
    const p = t.precision;
    head.replaceChildren(h("h1", { text: t.symbol }), statusBadge(t.status),
      h("span", { class: "muted" }, t.signal_id ? link(`/signal/${t.signal_id}`, t.signal_id) : "own trade", ` · ${t.trade_ref} · entered ${when(t.entry_ms)} London`));
    const pnl = h("div", { class: "card" }, h("h2", { text: "P&L" }), h("dl", { class: "kv" },
      h("dt", { text: t.status === "CLOSED" ? "Net (confirmed)" : "Unconfirmed (mark-to-market)" }),
      h("dd", { class: "big " + moneyCls(t.status === "CLOSED" ? t.net_pnl : t.mtm_pnl), text: money(t.status === "CLOSED" ? t.net_pnl : t.mtm_pnl) }),
      h("dt", { text: "Confirmed from logged exits" }), h("dd", { class: moneyCls(t.confirmed_pnl), text: money(t.confirmed_pnl) }),
      h("dt", { text: "R multiple" }), h("dd", { text: rFmt(t.r_multiple) }),
      h("dt", { text: "Risk at initial stop" }), h("dd", { text: money(-t.risk_usd) }),
      h("dt", { text: "Funding" }), h("dd", { text: money(t.funding_usd) }),
      h("dt", { text: "Plan would have made" }), h("dd", { text: money(t.plan_pnl) }),
      h("dt", { text: "Entry vs signal" }), h("dd", { text: t.entry_gap_pct != null ? sgn(t.entry_gap_pct, 2, "%") : "–" })),
      h("p", { class: "small muted", text: "Fees and funding included. Confirmed P&L uses only the exits you logged." }));
    const pos = h("div", { class: "card" }, h("h2", { text: "Position" }), h("dl", { class: "kv" },
      h("dt", { text: "Entry" }), h("dd", { text: `${price(t.entry_price, p)} (${t.entry_order})` }),
      h("dt", { text: "Size" }), h("dd", { text: `$${fx(t.margin, 2)} × ${t.leverage}x = $${fx(t.notional, 2)}` }),
      h("dt", { text: "Open" }), h("dd", { text: `${fx(t.qty_open / t.qty * 100, 1)}% (${t.qty_open.toPrecision(6)})` }),
      h("dt", { text: "Your SL" }), h("dd", { text: price(t.current_stop, p) }),
      h("dt", { text: "Suggested SL (plan)" }), h("dd", { class: "warn-t", text: t.suggested_stop != null ? price(t.suggested_stop, p) : "–" }),
      h("dt", { text: "TP1 / TP2" }), h("dd", { text: `${price(t.tp1, p)} / ${t.tp2 != null ? price(t.tp2, p) : "dropped"}` }),
      h("dt", { text: "Liquidation (est., mark)" }), h("dd", { class: "neg", text: price(t.liq_price, p) })),
      t.notes ? h("p", { class: "small", text: "Notes: " + t.notes }) : null);
    const pend = t.pending.length ? h("div", { class: "card needs" }, h("h2", { text: "To do / confirm" }), h("ul", {}, t.pending.map((x) => h("li", { text: x })))) : null;
    const warn = (t.warnings || []).length ? h("div", { class: "stack" }, t.warnings.map((w) => h("div", { class: "notice", text: w }))) : null;
    const chartEl = h("div", { class: "chart", role: "img", "aria-label": `${t.symbol} 15 minute chart with your trade levels` });
    const planTl = h("div", { class: "card" }, h("h2", { text: "Plan timeline (exit engine on your entry)" }),
      t.plan_events.length ? h("ul", { class: "conds" }, t.plan_events.map((e) => h("li", {},
        h("span", {}, h("span", { class: "num muted", text: clock(e.ts).slice(0, 5) + "  " }), e.kind.replace("_", " ") + (e.fraction ? ` ${fx(e.fraction * 100, 0)}%` : "") + (e.note ? ` – ${e.note}` : "")),
        h("span", { class: "num", text: price(e.kind === "STOP_MOVE" ? e.stop_after : e.price, p) })))) : h("div", { class: "muted small", text: "Nothing yet." }));
    const logged = h("div", { class: "card" }, h("h2", { text: "What you logged" }), eventsTable(t, () => load()));
    const audit = h("details", { class: "card" }, h("summary", { text: `Audit trail (${t.audit.length} entries, append-only)` }),
      h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Logged (London)", "Kind", "Event time", "Price", "Qty", "Change", "Note"].map((x) => h("th", { class: "left", text: x })))),
        h("tbody", {}, t.audit.map((a) => h("tr", {}, h("td", { class: "left num", text: when(a.logged_ms) }), h("td", { class: "left", text: a.kind }),
          h("td", { class: "left num", text: when(a.ts) }), h("td", { class: "left num", text: a.price != null ? price(a.price, p) : "" }),
          h("td", { class: "left num", text: a.qty != null ? a.qty.toPrecision(6) : "" }),
          h("td", { class: "left small", text: a.old || a.new ? JSON.stringify({ old: a.old, new: a.new }) : "" }), h("td", { class: "left small", text: a.note || "" })))))));
    body.replaceChildren(
      h("div", { class: "stack" }, pend, warn,
        h("div", { class: "card" }, h("div", { class: "row spread" }, h("h2", { text: "15m chart (live)" }), tradeButtons(t, () => load())), chartEl,
          legend([["#e4e7ec", "entry"], ["#ef5350", "your SL"], ["#f0b429", "suggested SL"], ["#8e2c2a", "liquidation"], ["#26a69a", "TP1 / TP2"]])),
        planTl, logged, audit),
      h("div", { class: "stack" }, pnl, pos));
    try {
      const c = await api(`/api/chart/${t.symbol}`);
      if (chartObj) chartObj.destroy();
      c.precision = c.precision ?? p;
      chartObj = makeChart(chartEl, c, { levels: [], box: null, lines: [
        { price: t.entry_price, color: "#e4e7ec", title: "entry" },
        { price: t.current_stop, color: "#ef5350", title: "your SL", width: 2 },
        { price: t.suggested_stop, color: "#f0b429", title: "suggested", style: 2 },
        { price: t.liq_price, color: "#8e2c2a", title: "liq", width: 2 },
        { price: t.tp1, color: "#26a69a", title: "TP1" }, { price: t.tp2, color: "#26a69a", title: "TP2" }],
        markerTime: Math.floor(t.entry_ms / 900000) * 900, markerText: "entry" });
    } catch (e) { chartEl.replaceChildren(errorBox(e)); }
  }
  load(true);
  timer = setInterval(() => { if (!document.querySelector("dialog[open]")) load(); }, 15000);
  return {
    onPrices: (m) => { if (chartObj && sym && S.prices[sym]) chartObj.tick(S.prices[sym].last ?? S.prices[sym].mark, m.ts); },
    destroy: () => { clearInterval(timer); if (chartObj) chartObj.destroy(); },
  };
}

function eventsTable(t, after) {
  const evs = t.effective || [];
  if (!evs.length) return h("div", { class: "muted small", text: "No exits or stop moves logged yet." });
  const p = t.precision;
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["Time (London)", "What", "Price", "Qty", ""].map((x, i) => h("th", { class: i < 2 ? "left" : "", text: x })))),
    h("tbody", {}, evs.map((e) => h("tr", { class: e.void ? "voided" : "" },
      h("td", { class: "left num", text: when(e.ts) }), h("td", { class: "left", text: e.kind.replace("_", " ").toLowerCase() + (e.void ? " (withdrawn)" : "") + (e.kind !== "STOP_MOVE" ? ` · ${e.order}` : "") }),
      h("td", { class: "num", text: price(e.price, p) }), h("td", { class: "num", text: e.qty != null ? (+e.qty).toPrecision(6) : "" }),
      h("td", {}, e.void ? "" : h("div", { class: "row" },
        h("button", { class: "btn small-btn", type: "button", text: "Correct", onclick: () => dialogForm("Correct logged event", [
          { name: "price", label: "Price", type: "number", step: "any", value: e.price },
          { name: "time", label: "Time (London)", type: "datetime-local", value: londonInput(e.ts) },
          ...(e.kind === "PARTIAL_CLOSE" ? [{ name: "qty", label: "Quantity", type: "number", step: "any", value: e.qty }] : []),
          { name: "note", label: "Reason", value: "" },
        ], "Save correction", async (v) => { await post(`/api/trade/${t.trade_ref}/event`, { event_id: e.id, ...v }); after(); }, "The original stays in the audit trail.") }),
        h("button", { class: "btn small-btn", type: "button", text: "Withdraw", onclick: () => dialogForm("Withdraw this entry?", [
          { name: "note", label: "Reason", value: "logged by mistake" },
        ], "Withdraw", async (v) => { await post(`/api/trade/${t.trade_ref}/event`, { event_id: e.id, void: true, ...v }); after(); }, "It stays in the audit trail but no longer counts.") }))))))));
}

// ---------------------------------------------------------------- Add own trade
function newTradeView(app) {
  app.append(h("div", { class: "card stack", style: "max-width:900px" }, h("h2", { text: "Add own trade (no signal)" }), tradeForm({ signal: null })));
}

// ---------------------------------------------------------------- Stats
const METRIC_ROWS = [
  ["n", "Trades", (m) => String(m.n)], ["win_rate", "Win rate", (m) => pct0(m.win_rate)],
  ["avg_r", "Average R", (m) => rFmt(m.avg_r)], ["expectancy_usd", "Expectancy $", (m) => money(m.expectancy_usd)],
  ["expectancy_r", "Expectancy R", (m) => rFmt(m.expectancy_r)],
  ["profit_factor", "Profit factor", (m) => (isNum(m.profit_factor) ? m.profit_factor.toFixed(2) : m.n ? "∞" : "–")],
  ["net", "Net $", (m) => money(m.net)], ["max_consec_losses", "Max losing streak", (m) => String(m.max_consec_losses ?? "–")],
  ["max_drawdown_usd", "Max drawdown $", (m) => (isNum(m.max_drawdown_usd) ? (m.max_drawdown_usd > 0 ? "-$" : "$") + m.max_drawdown_usd.toFixed(2) : "–")],
];
function metricsTable(cols) {   // cols: [[label, metrics]]
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", { class: "left", text: "" }), cols.map(([l]) => h("th", { text: l })))),
    h("tbody", {}, METRIC_ROWS.map(([k, label, f]) => h("tr", {}, h("td", { class: "left muted", text: label }),
      cols.map(([, m]) => h("td", { class: "num " + (k === "net" || k === "expectancy_usd" ? moneyCls(m[k]) : ""), text: m && m.n ? f(m) : (k === "n" ? "0" : "–") })))))));
}
function breakdownTable(obj) {   // {bucket: metrics}
  const keys = Object.keys(obj || {});
  if (!keys.length) return h("div", { class: "muted small", text: "No closed trades yet." });
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["Bucket", "Trades", "Win", "Avg R", "Exp $", "PF", "Net $"].map((x, i) => h("th", { class: i ? "" : "left", text: x })))),
    h("tbody", {}, keys.map((k) => { const m = obj[k]; return h("tr", {}, h("td", { class: "left", text: k }), h("td", { class: "num", text: String(m.n) }),
      h("td", { class: "num", text: pct0(m.win_rate) }), h("td", { class: "num", text: rFmt(m.avg_r) }), h("td", { class: "num " + moneyCls(m.expectancy_usd), text: money(m.expectancy_usd) }),
      h("td", { class: "num", text: isNum(m.profit_factor) ? m.profit_factor.toFixed(2) : "∞" }), h("td", { class: "num " + moneyCls(m.net), text: money(m.net) })); }))));
}
function statsView(app) {
  const range = h("select", { id: "st_range", "aria-label": "Period" }, [["0", "All time"], ["30", "Last 30 days"], ["7", "Last 7 days"], ["1", "Last 24 hours"]].map(([v, t]) => h("option", { value: v }, t)));
  const dim = h("select", { id: "st_dim", "aria-label": "Break down by" }, [["setup", "Setup"], ["regime", "Regime"], ["score", "Score bucket"], ["session", "Session"], ["hour", "Hour (London)"], ["alert", "Alert sent / suppressed"]].map(([v, t]) => h("option", { value: v }, t)));
  const box = h("div", { class: "stack" });
  app.append(h("div", { class: "stack" }, h("div", { class: "row spread" }, h("h2", { text: "Stats", style: "margin:0" }), h("div", { class: "row" }, range)), box));
  let data = null;
  async function load() {
    box.replaceChildren(h("div", { class: "muted", text: "Loading…" }));
    try { data = await api(`/api/stats?days=${range.value}`); draw(); } catch (e) { box.replaceChildren(errorBox(e)); }
  }
  function draw() {
    const s = data.system, m = data.mine;
    const bd = h("div", { class: "grid-2 even" },
      h("div", {}, h("h3", { text: "Policy L" }), breakdownTable(s.breakdowns[dim.value]?.L)),
      h("div", {}, h("h3", { text: "Policy S" }), breakdownTable(s.breakdowns[dim.value]?.S)));
    const myDim = ["setup", "regime", "score", "session", "hour"].includes(dim.value) ? breakdownTable(m.breakdowns[dim.value]) : h("div", { class: "muted small", text: "n/a for your trades" });
    const tvn = m.taken_vs_not_taken;
    box.replaceChildren(
      h("div", { class: "card stack" }, h("h2", { text: "System baseline (paper, every signal)" }),
        h("p", { class: "muted small", style: "margin:0", text: `${data.signals} signals recorded in this period. Both stop policies are paper-traded on every signal, sent or suppressed, at default size with fees, slippage and funding.` }),
        metricsTable([["Policy L", s.overall.L], ["Policy S", s.overall.S]]),
        h("div", { class: "row" }, h("h3", { text: "Break down by", style: "margin:0" }), dim), bd),
      h("div", { class: "card stack" }, h("h2", { text: "My trades" }),
        metricsTable([["All", m.overall], ["From signals", m.by_source.signal], ["Own trades", m.by_source.own]]),
        h("h3", { text: "Breakdown (same dimension)" }), myDim),
      h("div", { class: "grid-2 even" },
        h("div", { class: "card stack" }, h("h2", { text: "Does my selection beat the system?" }),
          h("p", { class: "muted small", style: "margin:0", text: "System results on signals you took vs signals you skipped." }),
          metricsTable([["L · taken", tvn.L.taken], ["L · skipped", tvn.L.not_taken], ["S · taken", tvn.S.taken], ["S · skipped", tvn.S.not_taken]]),
          h("h3", { text: "You vs the system on the signals you took" }),
          metricsTable([["You", m.you_vs_system_on_taken.you], ["System L", m.you_vs_system_on_taken.system_L], ["System S", m.you_vs_system_on_taken.system_S]])),
        h("div", { class: "card stack" }, h("h2", { text: "Execution" }),
          h("h3", { text: "Entry gap (your entry vs signal reference)" }),
          h("dl", { class: "kv" }, h("dt", { text: "Trades" }), h("dd", { text: String(m.entry_gap.n) }),
            h("dt", { text: "Average" }), h("dd", { text: sgn(m.entry_gap.avg_pct, 2, "%") }),
            h("dt", { text: "Median" }), h("dd", { text: sgn(m.entry_gap.median_pct, 2, "%") }),
            h("dt", { text: "Worst" }), h("dd", { text: sgn(m.entry_gap.worst_pct, 2, "%") })),
          h("h3", { text: "Plan adherence" }),
          h("p", { class: "muted small", style: "margin:0", text: m.plan_adherence.note }),
          h("dl", { class: "kv" }, h("dt", { text: "Trades" }), h("dd", { text: String(m.plan_adherence.n) }),
            h("dt", { text: "Total vs plan" }), h("dd", { class: moneyCls(m.plan_adherence.total_usd), text: money(m.plan_adherence.total_usd) }),
            h("dt", { text: "Average vs plan" }), h("dd", { class: moneyCls(m.plan_adherence.avg_usd), text: money(m.plan_adherence.avg_usd) }),
            h("dt", { text: "Beat the plan / worse" }), h("dd", { text: `${m.plan_adherence.better} / ${m.plan_adherence.worse}` })))),
      paperRecent());
  }
  function paperRecent() {
    const c = h("div", { class: "card" }, h("h2", { text: "Recent paper trades" }), h("div", { class: "muted small", text: "Loading…" }));
    api("/api/paper?limit=100").then((rows) => c.replaceChildren(h("h2", { text: "Recent paper trades" }), rows.length ? paperTable(rows) : h("div", { class: "muted", text: "No paper trades yet - they open automatically with every signal." }))).catch(() => {});
    return c;
  }
  range.addEventListener("change", load);
  dim.addEventListener("change", () => data && draw());
  load();
}

// ---------------------------------------------------------------- register routes + boot
routes.push(
  [/^\/trades$/, tradesView],
  [/^\/trades\/new$/, newTradeView],
  [/^\/trades\/close-all$/, closeAllView],
  [/^\/trade\/(M-\d+)$/, tradeView],
  [/^\/stats$/, statsView],
);
boot();
