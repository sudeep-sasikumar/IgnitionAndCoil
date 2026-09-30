/* Research tab: what the pattern miner found (history = two years rebuilt; live = recorded days). */
"use strict";

const RS = { which: loadPref("research.which", "history") };

function rNum(x, d = 2, suf = "") { return isNum(x) ? x.toFixed(d) + suf : "–"; }
function rPct(x) { return isNum(x) ? (x * 100).toFixed(0) + "%" : "–"; }
function rCls(x) { return !isNum(x) ? "faint" : x > 0 ? "pos" : x < 0 ? "neg" : ""; }
function rTable(head, rows) {
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, head.map((t, i) => h("th", { class: i === 0 ? "left" : "", text: t })))),
    h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c, i) => h("td", { class: i === 0 ? "left" : "num" }, c)))))));
}

function researchView(app) {
  const body = h("div", { class: "stack" });
  const pick = h("span", { class: "row" });
  const files = h("div");
  app.append(h("div", { class: "stack" },
    h("div", { class: "toolbar" }, h("h2", { text: "Research", style: "margin:0" }), pick),
    h("p", { class: "muted small", text: "Every 5 minutes the scanner records what it saw for every coin - features, each condition, score, decision, order book, OI, premium. The miner labels each row with what happened next and keeps only patterns that hold in BOTH halves of the data (history: 2024-25 vs 2025-26). Findings are evidence to test, not rules to trade blindly." }),
    body, h("div", { class: "card" }, h("h2", { text: "Recorded live data" }), files)));

  let data = null;
  function drawPick() {
    pick.replaceChildren(...[["history", "History (2 years)"], ["live", "Live recordings"]].map(([k, label]) =>
      h("button", { class: "btn small-btn" + (RS.which === k ? " primary" : ""), type: "button", text: label,
        onclick: () => { RS.which = k; savePref("research.which", k); drawPick(); draw(); } })));
  }
  function draw() {
    body.replaceChildren();
    const rep = data && data.reports[RS.which];
    if (!rep) {
      body.append(h("div", { class: "card" }, h("p", { class: "muted", text: RS.which === "live"
        ? "No live report yet: it is built every night once 3 days are recorded (research.mine_daily_at)."
        : "No history report yet: python -m research.history ... then python -m research.mine --history y2025,y2026" })));
      return;
    }
    const P = rep.overview.map((o) => o.period);
    body.append(h("p", { class: "muted small", text: `Generated ${when(rep.generated_ms)} · config ${rep.config} · win proxy = +${rep.params.tp_pct}% before -${rep.params.sl_pct}% within 4h · big move = +${rep.params.big_move_pct}% within 4h · breakout outcome = ${rep.params.breakout_target === "trade_r" ? "the exact Policy S trade (R)" : "the win proxy"}` }));
    body.append(h("div", { class: "card" }, h("h3", { text: "Overview" }), rTable(
      ["Period", "Rows", "Coins", "Days", "Breakouts", "Signals", "Sent", "Win proxy (any bar)", "Big-move rate"],
      rep.overview.map((o) => [o.period, o.rows.toLocaleString("en-GB"), String(o.coins), rNum(o.days, 0), o.breakouts.toLocaleString("en-GB"),
        String(o.signals), String(o.sent), rPct(o.tp_first_rate), rPct(o.big_move_rate)]))));

    const nm = rep.near_misses;
    const oc = (v) => v && v.note ? "no data" : v ? (v.trade_r != null ? h("span", { class: "num " + rCls(v.trade_r), text: `${v.n} · ${rNum(v.trade_r, 2, "R")}` }) : h("span", { class: "num", text: `${v.n} · ${rPct(v.tp_first)}` })) : "–";
    body.append(h("div", { class: "card" }, h("h3", { text: "Near misses: breakouts blocked by exactly one active condition" }),
      h("p", { class: "muted small", text: "If a blocker's near misses do as well as the signals that fired, that filter may be costing trades. Cells: count · average outcome." }),
      rTable(["Blocked only by", ...P], [...nm.blockers.map((b) => [b.condition, ...P.map((p) => oc(b[p]))]),
        [h("b", { text: "Signals that fired" }), ...P.map((p) => oc(nm.fired[p]))]])));

    const ed = rep.edges.filter((e) => e.consistent);
    body.append(h("div", { class: "card" }, h("h3", { text: "What separates winning from losing breakouts (consistent in both periods)" }),
      ed.length ? rTable(["Feature", "Direction", "Rank correlation", ...P.map((p) => `${p}: worst → best decile`)],
        ed.map((e) => [e.feature, e.direction, (e.rho || []).map((r) => rNum(r, 2)).join(" / "),
          ...P.map((p) => { const v = (e.by_decile[p] || []).filter(isNum); return v.length ? `${rNum(Math.min(...v))} → ${rNum(Math.max(...v))}` : "–"; })]))
        : h("p", { class: "muted", text: "No feature was consistent in both periods." })));

    const mm = rep.missed_moves;
    body.append(h("div", { class: "card" }, h("h3", { text: `Big moves (+${mm.big_move_pct}% within 4h): caught or missed?` }),
      rTable(["Period", "Big moves", "Signalled within ±1h"], Object.entries(mm.by_period).map(([p, v]) => [p, String(v.moves), `${v.caught} (${rPct(v.caught / Math.max(v.moves, 1))})`])),
      h("p", { class: "muted small", text: "Why the missed ones were missed:" }),
      rTable(["Reason", "Moves"], Object.entries(mm.why_missed).map(([k, v]) => [k, String(v)])),
      h("p", { class: "muted small", text: "What the start of a big move looks like vs an ordinary bar (standardised difference; ±0.2 = noticeable, ±0.5 = strong):" }),
      rTable(["Feature", ...P, "Consistent"], mm.profile.map((x) => [x.feature, ...x.smd.map((v) => h("span", { class: "num " + rCls(v), text: rNum(v, 2) })), x.consistent ? "yes" : ""]))));

    const sed = (rep.signal_edges || []).filter((e) => e.consistent);
    if (sed.length) body.append(h("div", { class: "card" }, h("h3", { text: "What separates winning from losing SENT signals (consistent in both periods)" }),
      rTable(["Feature", "Direction", "Rank correlation"], sed.map((e) => [e.feature, e.direction, (e.rho || []).map((r) => rNum(r, 2)).join(" / ")]))));
    for (const key of ["rules_signals", "rules_breakout", "rules_tp", "rules_big"]) {
      const rr = rep[key];
      if (!rr || !rr.rules) continue;
      const A = rr.found_on, B = rr.tested_on, ratio = (rr.measure || "").startsWith("lift");
      const eff = (v) => !v ? "too few" : `${v.n} · ${rNum(v.rate, 3)} · ${ratio ? rNum(v.effect, 2) + "×" : (v.effect > 0 ? "+" : "") + rNum(v.effect, 3) + "R"}`;
      const tbl = (list) => rTable(["Rule", `${A}: n · result · vs base`, `${B}: n · result · vs base`, "Holds"],
        list.map((x) => [x.rule, eff(x[A]), eff(x[B]), x.holds ? h("span", { class: "pos", text: "yes" }) : ""]));
      body.append(h("div", { class: "card" }, h("h3", { text: `Rules for: ${rr.target}` }),
        h("p", { class: "muted small", text: `Found on ${A}, tested on ${B} (${rr.measure}). Base: ${Object.entries(rr.base).map(([p, v]) => `${p} ${rNum(v, 3)}`).join(", ")}. "Holds" = the effect is still there on the period it was not found on.` }),
        h("h4", { text: "Better than the base" }), tbl(rr.rules.filter((x) => x.holds).slice(0, 10)),
        h("h4", { text: "Worse than the base (avoid)" }), tbl((rr.avoid || []).filter((x) => x.holds).slice(0, 10))));
    }
    const cand = data.reports.candidates;
    if (RS.which === "history" && cand) {
      const names = Object.keys(cand), per = Object.keys(cand[names[0]] || {});
      const cell = (x) => x && x.n ? h("span", { class: "num " + rCls(x.R), text: `${x.n} · ${x.R > 0 ? "+" : ""}${x.R}R · $${x.net} · PF ${x.pf ?? "–"} · DD ${x.maxdd_R}R` }) : "–";
      body.prepend(h("div", { class: "card" }, h("h3", { text: "Candidate systems (live alert rules, exact Policy S trades)" }),
        h("p", { class: "muted small", text: `Rules found on ${per[0]} and replayed as systems with cooldown, hourly cap and "Policy S must fit"; ${per[per.length - 1]} is the year they were not found on. Cells: trades · total R · net $ at $1,000 · profit factor · worst drawdown.` }),
        rTable(["System", ...per], names.map((n) => [n, ...per.map((p) => cell(cand[n][p]))]))));
    }
    if (rep.live_only && rep.live_only.length) {
      body.append(h("div", { class: "card" }, h("h3", { text: "Live-only features (order book, premium, OI)" }),
        rTable(["Feature", "Direction", "Rank correlation", "Consistent"], rep.live_only.map((e) => [e.feature, e.direction, (e.rho || []).map((r) => rNum(r, 2)).join(" / "), e.consistent ? "yes" : ""]))));
    }
  }
  function drawFiles() {
    if (!data) return;
    files.replaceChildren(!data.recording ? h("p", { class: "muted", text: "Recording is off (research.record_snapshots)." })
      : data.files.length ? h("div", { class: "small" }, h("p", { class: "muted", text: `${data.files.length} day(s) recorded. One CSV per UTC day - open in Excel or pandas.` }),
        h("div", { class: "row", style: "flex-wrap:wrap" }, data.files.slice(-30).reverse().map((f) =>
          h("a", { class: "btn small-btn", href: `/api/research/file/${f.name}`, text: `${f.name.replace(".csv.gz", "")} (${(f.bytes / 1e6).toFixed(1)} MB)` }))))
      : h("p", { class: "muted", text: "Recording started - the first file appears after the next 5-minute bar." }));
  }
  async function load() {
    try { data = await api("/api/research"); } catch (err) { body.replaceChildren(h("p", { class: "neg", text: "Could not load: " + err.message })); return; }
    draw(); drawFiles();
  }
  drawPick(); load();
}

routes.push([/^\/research$/, researchView]);
