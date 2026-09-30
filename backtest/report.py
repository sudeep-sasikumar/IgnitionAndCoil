"""Self-contained HTML backtest report (inline CSS + SVG, no scripts). Uses the same metrics as
the live stats page (stats.metrics)."""
from __future__ import annotations

import html
import math
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from stats.metrics import DIMENSIONS, breakdown, metrics

E = html.escape


def _t(ms, tz, fmt="%Y-%m-%d %H:%M"):
    if not ms:
        return "–"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz).strftime(fmt)


def _money(x):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "–"
    return ("-$" if x < 0 else "+$") + f"{abs(x):,.2f}"


def _r(x):
    return "–" if x is None else f"{x:+.2f}R"


def _pct(x):
    return "–" if x is None else f"{x * 100:.0f}%"


def _pf(m):
    pf = m.get("profit_factor")
    if pf is None:
        return "–"
    return "∞" if pf == math.inf else f"{pf:.2f}"


def _cls(x):
    return "" if x is None else ("pos" if x > 0 else "neg" if x < 0 else "")


ROWS = [("Trades", lambda m: str(m.get("n", 0))), ("Win rate", lambda m: _pct(m.get("win_rate"))),
        ("Average R", lambda m: _r(m.get("avg_r"))), ("Expectancy $", lambda m: _money(m.get("expectancy_usd"))),
        ("Expectancy R", lambda m: _r(m.get("expectancy_r"))), ("Profit factor", _pf),
        ("Net $", lambda m: _money(m.get("net"))), ("Avg win / loss", lambda m: f"{_money(m.get('avg_win'))} / {_money(m.get('avg_loss'))}"),
        ("Max losing streak", lambda m: str(m.get("max_consec_losses", "–"))),
        ("Max drawdown $", lambda m: "–" if m.get("max_drawdown_usd") is None else f"-${m['max_drawdown_usd']:,.2f}")]


def metrics_table(cols: list[tuple[str, dict]]) -> str:
    head = "".join(f"<th>{E(c)}</th>" for c, _ in cols)
    body = "".join("<tr><td class=l>" + E(label) + "</td>" +
                   "".join(f"<td>{E(f(m)) if m.get('n') else ('0' if label == 'Trades' else '–')}</td>" for _, m in cols) +
                   "</tr>" for label, f in ROWS)
    return f"<table><thead><tr><th></th>{head}</tr></thead><tbody>{body}</tbody></table>"


def breakdown_table(b: dict) -> str:
    if not b:
        return "<p class=muted>No closed trades.</p>"
    rows = "".join(
        f"<tr><td class=l>{E(k)}</td><td>{m['n']}</td><td>{_pct(m.get('win_rate'))}</td><td>{_r(m.get('avg_r'))}</td>"
        f"<td class='{_cls(m.get('expectancy_usd'))}'>{_money(m.get('expectancy_usd'))}</td><td>{_pf(m)}</td>"
        f"<td class='{_cls(m.get('net'))}'>{_money(m.get('net'))}</td></tr>" for k, m in b.items())
    return ("<table><thead><tr><th class=l>Bucket</th><th>Trades</th><th>Win</th><th>Avg R</th><th>Exp $</th>"
            f"<th>PF</th><th>Net $</th></tr></thead><tbody>{rows}</tbody></table>")


def equity_svg(series: dict[str, list[dict]], tz) -> str:
    colors = {"L": "#ef5350", "S": "#ff9f43"}
    pts_all = {}
    for pol, trades in series.items():
        eq, pts = 0.0, []
        for t in sorted(trades, key=lambda x: x["exit_ms"]):
            eq += t["net"]
            pts.append((t["exit_ms"], eq))
        pts_all[pol] = pts
    allp = [p for v in pts_all.values() for p in v]
    if not allp:
        return "<p class=muted>No closed trades to plot.</p>"
    W, H, pad = 900, 260, 44
    t0, t1 = min(p[0] for p in allp), max(p[0] for p in allp)
    lo, hi = min(0.0, min(p[1] for p in allp)), max(0.0, max(p[1] for p in allp))
    t1 = t1 if t1 > t0 else t0 + 1
    hi = hi if hi > lo else lo + 1
    X = lambda t: pad + (t - t0) / (t1 - t0) * (W - 2 * pad)
    Y = lambda v: H - pad - (v - lo) / (hi - lo) * (H - 2 * pad)
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Cumulative net P&amp;L by exit time">',
             f'<line x1="{pad}" x2="{W - pad}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" class=zero />']
    for v in (lo, hi):
        parts.append(f'<text x="4" y="{Y(v) + 4:.1f}" class=ax>{E(_money(v))}</text>')
    parts.append(f'<text x="{pad}" y="{H - 12}" class=ax>{E(_t(t0, tz, "%d %b"))}</text>')
    parts.append(f'<text x="{W - pad}" y="{H - 12}" class=ax text-anchor="end">{E(_t(t1, tz, "%d %b"))}</text>')
    for pol, pts in pts_all.items():
        if pts:
            d = " ".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in [(t0, 0.0)] + pts)
            parts.append(f'<polyline points="{d}" fill="none" stroke="{colors.get(pol, "#5b9dff")}" stroke-width="2" />')
    parts.append("</svg>")
    legend = " ".join(f'<span class=lg><i style="background:{colors.get(p)}"></i>Policy {p}</span>' for p in pts_all)
    return "".join(parts) + f"<div class=legend>{legend}</div>"


def funnel_html(f: dict, cfg) -> str:
    if not f:
        return "<p class=muted>n/a</p>"
    titles = {"IGNITION": "Ignition (every symbol, every 5m bar)", "COIL_WATCH": "Coil WATCH (every symbol, every 5m bar)",
              "COIL_ENTRY": "Coil ENTRY (15m closes while watching)"}
    parts = ["<p class=muted>Pass rate = share of evaluations where the condition held (disabled conditions count as "
             "passing). <b>Sole blocker</b> = near-misses where every other condition held and this one alone failed - "
             "the conditions to look at first when tuning.</p><div class=grid>"]
    for k in ("IGNITION", "COIL_WATCH", "COIL_ENTRY"):
        g = f.get(k) or {}
        if not g.get("n"):
            parts.append(f"<div><b>{E(titles[k])}</b><p class=muted>never evaluated</p></div>")
            continue
        rows = "".join(f"<tr><td class=l>{E(c['name'])}</td><td>{c['pass_rate'] * 100:.1f}%</td><td>{c['sole_blocker']:,}</td></tr>"
                       for c in sorted(g["conds"], key=lambda c: -c["sole_blocker"]))
        sb = g.get("score_buckets") or {}
        score_line = ""
        if k != "COIL_WATCH":
            score_line = (f"<p class=muted>All hard conditions passed {g['all_pass']:,} times; score of those: "
                          + ", ".join(f"{E(b)}: {sb.get(b, 0):,}" for b in ("<50", "50-59", "60-69", "70+"))
                          + f" (ENTRY needs ≥ {cfg.score.min_entry}; 2h cooldown per symbol also applies).</p>")
        else:
            score_line = f"<p class=muted>All WATCH conditions held {g['all_pass']:,} times.</p>"
        parts.append(f"<div><b>{E(titles[k])}</b> <span class=muted>- {g['n']:,} evaluations</span>{score_line}"
                     f"<table><thead><tr><th class=l>Condition</th><th>Pass rate</th><th>Sole blocker</th></tr></thead>"
                     f"<tbody>{rows}</tbody></table></div>")
    parts.append("</div>")
    na = f.get("stop_s_na") or {}
    if na:
        parts.append("<p class=muted><b>Policy S n/a:</b> " + "; ".join(f"{E(k)} ({v})" for k, v in na.items()) + "</p>")
    return "".join(parts)


CSS = """
:root{--bg:#0e1116;--panel:#161a22;--p2:#1c212b;--b:#29303c;--t:#e4e7ec;--m:#8a93a3;--g:#26a69a;--r:#ef5350;--a:#f0b429}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1200px;margin:0 auto;padding:20px 16px 40px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 10px}
h3{font-size:12px;color:var(--m);text-transform:uppercase;letter-spacing:.04em;margin:14px 0 6px}
.card{background:var(--panel);border:1px solid var(--b);border-radius:8px;padding:14px;margin-top:14px}
.muted{color:var(--m)}.pos{color:var(--g)}.neg{color:var(--r)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{padding:5px 8px;border-bottom:1px solid var(--b);text-align:right;white-space:nowrap}
th{color:var(--m);font-weight:500;background:var(--p2);font-size:12px}.l{text-align:left}
.wrap{overflow-x:auto}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
@media(max-width:800px){.grid{grid-template-columns:minmax(0,1fr)}}
.warn{border-left:3px solid var(--a);background:rgba(240,180,41,.1);padding:10px 12px;border-radius:4px}
.warn li{margin:4px 0}svg{width:100%;height:auto}svg .zero{stroke:#3a4150;stroke-dasharray:4 4}svg .ax{fill:var(--m);font-size:11px}
.legend{display:flex;gap:14px;font-size:12px;color:var(--m)}.lg i{display:inline-block;width:14px;height:3px;margin-right:5px;vertical-align:middle}
details summary{cursor:pointer;color:var(--m)}kbd{font-family:ui-monospace,Consolas,monospace;font-size:12px}
"""


def build(result: dict, meta: dict, cfg) -> str:
    tz = ZoneInfo(cfg.app.display_tz)
    trades = result["trades"]
    closed = [t for t in trades if t["status"] == "CLOSED"]
    by_pol = {p: [t for t in closed if t["policy"] == p] for p in ("L", "S")}
    open_end = Counter(t["policy"] for t in trades if t["status"] == "OPEN_AT_END")
    sigs = result["signals"]
    s_na = sum(1 for s in sigs if s["plan"]["stop_s"]["price"] is None)

    notes = "".join(f"<li>{n}</li>" for n in meta["notes"])
    summary = metrics_table([("Policy L", metrics(by_pol["L"])), ("Policy S", metrics(by_pol["S"]))])
    dims = ""
    for dim, label in (("side", "Long / short"), ("setup", "Setup"), ("regime", "Regime"), ("score", "Score bucket"), ("session", "Session"),
                       ("hour", "Hour of entry (London)"), ("alert", "Alert sent / suppressed")):
        fn = DIMENSIONS[dim] if dim != "side" else (lambda t: t.get("side", "LONG"))
        dims += (f"<h3>{E(label)}</h3><div class=grid><div class=wrap><b>Policy L</b>{breakdown_table(breakdown(by_pol['L'], fn))}</div>"
                 f"<div class=wrap><b>Policy S</b>{breakdown_table(breakdown(by_pol['S'], fn))}</div></div>")
    reasons = ""
    for p in ("L", "S"):
        c = Counter(t["exit_reason"] for t in by_pol[p])
        reasons += f"<div><b>Policy {p}</b><table><tbody>" + "".join(
            f"<tr><td class=l>{E(k)}</td><td>{v}</td><td>{v / max(len(by_pol[p]), 1) * 100:.0f}%</td></tr>"
            for k, v in c.most_common()) + "</tbody></table></div>"
    res_by_sig: dict[str, dict] = {}
    for t in trades:
        res_by_sig.setdefault(t["signal_id"], {})[t["policy"]] = t

    def cell(t):
        if t is None:
            return "<td class=muted>n/a</td>"
        if t["status"] != "CLOSED":
            return "<td class=muted>open at end</td>"
        return f"<td class='{_cls(t['net'])}'>{_money(t['net'])} ({_r(t['r'])}) {E(t['exit_reason'] or '')}</td>"
    sig_rows = "".join(
        f"<tr><td class=l>{E(s['signal_id'])}</td><td class=l>{E(_t(s['bar_close_ms'], tz))}</td><td class=l>{E(s['symbol'])}</td>"
        f"<td class=l>{E(s['setup'])}</td><td>{s['score']}</td><td class=l>{E(s.get('regime', {}).get('state', '?'))}</td>"
        f"<td class=l>{E(s.get('suppressed_reason') or 'sent')}</td>{cell(res_by_sig.get(s['signal_id'], {}).get('L'))}"
        f"{cell(res_by_sig.get(s['signal_id'], {}).get('S'))}</tr>" for s in sigs)
    setups = Counter(s["setup"] for s in sigs)

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Backtest {E(_t(result['start_ms'], tz, '%Y-%m-%d'))} to {E(_t(result['end_ms'], tz, '%Y-%m-%d'))} · Ignition &amp; Coil</title><style>{CSS}</style></head>
<body><main>
<h1>Backtest report</h1>
<div class=muted>{E(_t(result['start_ms'], tz))} → {E(_t(result['end_ms'], tz))} London · {result['bars']:,} five-minute steps ·
{len(meta['symbols'])} symbols · config {E(meta['config_hash'])} · generated {E(_t(meta['generated_ms'], tz))} · runtime {meta['runtime_s']:.0f}s</div>

<div class="card warn"><h2>Data &amp; limitations - read first</h2><ul>{notes}</ul></div>

<div class=card><h2>Summary (same metrics as the live Stats page)</h2>
<p class=muted>{len(sigs)} ENTRY signals ({', '.join(f'{k} {v}' for k, v in setups.items()) or 'none'}). Policy S was n/a on {s_na}.
Trades still open at the end of the data (excluded): L {open_end.get('L', 0)}, S {open_end.get('S', 0)}.
Default size: ${cfg.trade.margin_usd:g} × {cfg.trade.leverage:g}x = ${cfg.trade.notional_usd:g}; fees maker {cfg.trade.maker_fee * 100:.3f}% / taker {cfg.trade.taker_fee * 100:.3f}%, slippage {cfg.trade.slippage_pct}% on market fills, funding included.</p>
<div class=wrap>{summary}</div></div>

<div class=card><h2>Condition funnel - why signals did or didn't fire</h2>{funnel_html(result.get("funnel") or {}, cfg)}</div>

<div class=card><h2>Equity (cumulative net $ by exit time)</h2>{equity_svg(by_pol, tz)}</div>

<div class=card><h2>Breakdowns</h2>{dims}</div>

<div class=card><h2>Exit reasons</h2><div class=grid>{reasons}</div></div>

<div class=card><details><summary>All {len(sigs)} signals and their L / S results</summary><div class=wrap><table>
<thead><tr><th class=l>Signal</th><th class=l>Time (London)</th><th class=l>Symbol</th><th class=l>Setup</th><th>Score</th><th class=l>Regime</th><th class=l>Alert</th><th>Policy L</th><th>Policy S</th></tr></thead>
<tbody>{sig_rows}</tbody></table></div></details></div>

<div class=card><h2>Symbols</h2><p class=muted>{E(', '.join(meta['symbols']))}</p></div>
</main></body></html>"""
