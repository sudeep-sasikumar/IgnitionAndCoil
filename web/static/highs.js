/* Highs tab: CoinGecko top coins breaking their 52-week / all-time high, plus the event study. */
"use strict";

const HS = { kind: loadPref("highs.kind", "ALL"), q: "" };

function usd(x) {
  if (!isNum(x)) return "–";
  for (const [d, s] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) if (Math.abs(x) >= d) return "$" + (x / d).toFixed(1) + s;
  return "$" + x.toFixed(0);
}
function ago(ms) {
  if (!ms) return "?";
  const d = (Date.now() - ms) / 86400000;
  return d >= 365 ? (d / 365).toFixed(1) + "y" : d >= 1 ? d.toFixed(0) + "d" : (d * 24).toFixed(0) + "h";
}
const pctCell = (x) => h("span", { class: "num " + cls(x, (v) => v > 0, (v) => v < 0), text: sgn(x, 1, "%") });

function highsView(app) {
  const status = h("div", { class: "muted small" });
  const wrap = h("div", { class: "table-wrap" });
  const studyBox = h("div");
  const search = h("input", { type: "search", placeholder: "Filter coin…", "aria-label": "Filter coins",
    oninput: (e) => { HS.q = e.target.value.trim().toLowerCase(); draw(); } });
  const kinds = [["ALL", "All"], ["ATH", "All-time high"], ["52W", "52-week high"]];
  const kindBtns = h("span", { class: "row" }, kinds.map(([k, label]) => h("button", {
    class: "btn small-btn" + (HS.kind === k ? " primary" : ""), type: "button", text: label,
    onclick: () => { HS.kind = k; savePref("highs.kind", k); for (const b of kindBtns.children) b.classList.toggle("primary", b.textContent === label); draw(); },
  })));
  app.append(h("div", { class: "stack" },
    h("div", {}, h("div", { class: "toolbar" }, h("h2", { text: "New highs", style: "margin:0" }), kindBtns, search), status, wrap,
      h("p", { class: "faint small", text: "CoinGecko top coins (stablecoins and wrapped / staked copies left out). A break counts when price trades above a high that is at least a week old: ATH = CoinGecko's all-time high, 52W = the highest high of the last 365 days (not an ATH). \"vs old high\" below 0% = the breakout failed so far. \"Peak since\" = the highest price since the break (hover for when), \"Max run-up\" = that peak vs the old high, \"Off peak\" = how far price is below the peak now." })),
    h("div", { class: "card" }, h("h2", { text: "What happened after past breaks" }), studyBox)));

  let data = null;
  function draw() {
    if (!data) { wrap.replaceChildren(h("div", { class: "empty", text: "Loading…" })); return; }
    if (data.disabled) { wrap.replaceChildren(h("div", { class: "empty", text: "The Highs scanner is off (highs.enabled in config.yaml)." })); return; }
    const st = data.status;
    status.replaceChildren(
      `Tracking ${st.tracked.toLocaleString("en-GB")} of the top ${st.top_n.toLocaleString("en-GB")} coins · last scan ${st.last_scan_ms ? when(st.last_scan_ms) : "pending"} (every ${st.scan_min} min) · `,
      h("span", { title: "A year of price history per coin is needed for the 52-week high; it loads a few coins per minute (all-time highs work immediately)" },
        `52-week history loaded for ${st.history_ready.toLocaleString("en-GB")}/${st.tracked.toLocaleString("en-GB")}`),
      st.keyed ? "" : " · keyless CoinGecko API",
      st.peaks_pending ? ` · loading the peak of ${st.peaks_pending} earlier break(s)` : "",
      st.error ? h("span", { class: "neg", text: " · last scan failed: " + st.error }) : "");
    let rows = data.events.filter((e) => (HS.kind === "ALL" || e.kind === HS.kind) &&
      (!HS.q || e.symbol.toLowerCase().includes(HS.q) || e.name.toLowerCase().includes(HS.q)));
    if (!rows.length) {
      wrap.replaceChildren(h("div", { class: "empty", text: data.events.length ? "No breaks match the filter." :
        `No breaks in the last ${st.show_days} days yet. The first scan only records each coin's highs; breaks are detected from the next scan on.` }));
      return;
    }
    const head = ["Time (London)", "Coin", "Rank", "Type", "Old high", "Set", "New high", "Peak since", "Max run-up", "Now", "Since break", "vs old high", "Off peak", "Mkt cap", "Vol 24h", "Links"];
    const tips = { "Peak since": "Highest price since the break", "Max run-up": "Peak vs the old high: how far it ran above the level it broke",
      "Since break": "Now vs the price when the break was detected", "vs old high": "Now vs the old high", "Off peak": "Now vs the peak: how much of the run it has given back" };
    wrap.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, head.map((t, i) => h("th", { class: i < 2 || i === head.length - 1 ? "left" : "", title: tips[t] || null, text: t })))),
      h("tbody", {}, rows.map((e) => h("tr", {},
        h("td", { class: "left num", text: when(e.ts) }),
        h("td", { class: "left" }, h("b", { text: e.symbol }), " ", h("span", { class: "muted small", text: e.name })),
        h("td", { class: "num", text: e.rank ? "#" + e.rank : "–" }),
        h("td", {}, h("span", { class: `badge ${e.kind === "ATH" ? "ATH" : "H52"}`, text: e.kind === "ATH" ? "ATH" : "52W" })),
        h("td", { class: "num", text: price(e.prev_high) }),
        h("td", { class: "num muted", title: when(e.prev_high_ms), text: ago(e.prev_high_ms) + " ago" }),
        h("td", { class: "num", text: price(e.level) }),
        h("td", { class: "num", title: e.peak_ms ? "Peak " + when(e.peak_ms) : "Loading from CoinGecko candles", text: price(e.peak) }),
        h("td", { title: isNum(e.runup_from_alert_pct) ? sgn(e.runup_from_alert_pct, 1, "%") + " from the price at the alert" : null }, pctCell(e.runup_pct)),
        h("td", { class: "num", text: price(e.price) }),
        h("td", {}, pctCell(e.since_break_pct)),
        h("td", {}, pctCell(e.vs_old_high_pct)),
        h("td", {}, pctCell(e.off_peak_pct)),
        h("td", { class: "num", text: usd(e.market_cap) }),
        h("td", { class: "num", text: usd(e.volume) }),
        h("td", { class: "left" },
          h("a", { href: `https://www.coingecko.com/en/coins/${encodeURIComponent(e.cg_id)}`, target: "_blank", rel: "noopener", text: "CoinGecko" }),
          e.weex_symbol ? [" · ", h("a", { href: `https://www.tradingview.com/chart/?symbol=${encodeURIComponent("WEEX:" + e.weex_symbol + ".P")}`, target: "_blank", rel: "noopener", text: "WEEX perp" })] : null),
      )))));
  }

  function drawStudy() {
    const s = data && data.study;
    if (!s) {
      studyBox.replaceChildren(h("p", { class: "muted", text: "No study yet. Run it on the PC or VPS: python -m highs.study (a few minutes; free Binance daily price history)." }));
      return;
    }
    const H = s.horizons;
    const pick = [7, 30, 90].filter((x) => H.includes(x));
    const table = (rows, first) => h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, [first, "Events", ...pick.flatMap((d) => [`${d}d avg`, `${d}d median`, `${d}d up`]), "30d vs BTC", "Max rise 30d", "Max drop 30d", "Held 30d", "Failed ≤3d"]
        .map((t, i) => h("th", { class: i === 0 ? "left" : "", text: t })))),
      h("tbody", {}, rows.map((g) => h("tr", {},
        h("td", { class: "left", text: g.label }),
        h("td", { class: "num", text: String(g.n) }),
        pick.flatMap((d) => { const x = g.ret[d] || {}; return [h("td", {}, pctCell(x.mean)), h("td", {}, pctCell(x.median)), h("td", { class: "num", text: isNum(x.win) ? (x.win * 100).toFixed(0) + "%" : "–" })]; }),
        h("td", {}, pctCell(g.excess30)),
        h("td", { class: "num", text: sgn(g.runup30_med, 1, "%") }),
        h("td", { class: "num", text: sgn(g.dd30_med, 1, "%") }),
        h("td", { class: "num", text: isNum(g.held30) ? (g.held30 * 100).toFixed(0) + "%" : "–" }),
        h("td", { class: "num", text: isNum(g.failed3) ? (g.failed3 * 100).toFixed(0) + "%" : "–" }))))));
    const sections = [["Overall", s.overall, "Group"], ["By market-cap rank (today's rank)", s.by_rank, "Group"],
      ["By BTC trend on the day", s.by_btc, "Group"], ["By year", s.by_year, "Group"]];
    studyBox.replaceChildren(
      h("p", { class: "muted small", text: `Generated ${when(s.generated_ms)} · ${s.coverage.coins_with_history} coins with ≥1 year of daily history (of ${s.coverage.cg_top} in the CoinGecko top; ${s.coverage.matched} on Binance) · entry = the daily close that broke the high · returns close-to-close · "Held 30d" = still above the old high after 30 days · "Failed ≤3d" = closed back below it within 3 days.` }),
      ...(s.headline || []).map((t) => h("p", { text: t })),
      ...sections.flatMap(([title, rows, first]) => rows && rows.length ? [h("h3", { text: title }), table(rows, first)] : []),
      h("details", {}, h("summary", { text: "Method and caveats" }), h("ul", { class: "small muted" }, (s.notes || []).map((n) => h("li", { text: n })))));
  }

  async function load() {
    try { data = await api("/api/highs"); } catch (err) { status.textContent = "Could not load: " + err.message; return; }
    draw(); drawStudy();
  }
  load();
  const timer = setInterval(load, 60000);
  draw();
  return { destroy: () => clearInterval(timer) };
}

routes.push([/^\/highs$/, highsView]);
