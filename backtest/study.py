"""Walk-forward study across leverage / stop-policy models.

    .venv\\Scripts\\python.exe -m backtest.study --days 365

1. Download history (cached) and run ONE harvest pass of the live code at the loosest grid
   settings (no score cut-off, no cooldown) -> every potential signal.
2. For every candidate, rebuild the trade plan at each leverage (same $1,000 notional, margin =
   notional / leverage) and simulate both stop policies with the live exit engine.
   Signals don't depend on leverage - only the liquidation price, the Policy L stop and
   whether Policy S fits do.
3. Walk-forward per model: tune the thresholds (backtest.sweep grid) on everything BEFORE a test
   window, apply them to the next window, move on. Only test-window trades count, so every
   reported trade comes from settings that never saw it.
Returns are compared in R (profit per unit of risk) and in $ at an equal $20 risk per trade -
the fair comparison when leverage changes the stop distance.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import html
import itertools
import json
import math
import random
import statistics as st
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.__main__ import _say, apply_overrides, prepare  # noqa: E402
from backtest.engine import Backtester  # noqa: E402
from backtest.report import CSS  # noqa: E402
from backtest.sweep import GRID, LOOSEST, neighbours, select  # noqa: E402
from core.clock import Clock  # noqa: E402
from core.config import Config, load_config  # noqa: E402
from core.logs import setup_logging  # noqa: E402
from data.db import Database, clean_json  # noqa: E402
from exchange.weex_rest import WeexRest  # noqa: E402
from plan.trade_plan import build_plan  # noqa: E402
from stats.metrics import metrics  # noqa: E402

DAY = 86_400_000
LEVERAGES = (50, 25, 20, 10)
RISK_USD = 20.0      # $ comparison: every trade scaled to risk $20


def model_keys() -> list[str]:
    return [f"{lev}x-{pol}" for lev in LEVERAGES for pol in ("L", "S")]


def resimulate(bt: Backtester, sig: dict, lev: float, end_ms: int, inp) -> dict[str, dict]:
    """Same signal, plan rebuilt at `lev` with the same notional, both policies simulated."""
    cfg = bt.cfg
    p = sig["plan"]
    notional = float(cfg.trade.notional_usd)
    plan = build_plan(sig["symbol"], sig["setup"], float(p["ref_entry"]), p.get("structural_stop"), p.get("resistance"),
                      inp.brackets.get(sig["symbol"]), cfg, margin=notional / lev, leverage=lev,
                      precision=inp.precision.get(sig["symbol"]))
    trades = bt.simulate({**sig, "plan": asdict(plan)}, end_ms)
    return {t["policy"]: t for t in trades}


def pick_combo(cands: list[dict], key: str, t_end: int, cooldown: int, min_trades: int) -> tuple[dict | None, dict]:
    """Best thresholds on candidates entered before t_end (in-sample), neighbourhood-smoothed."""
    keys = list(GRID)
    past = [c for c in cands if c["T"] < t_end]
    total: dict[tuple, float] = {}
    info: dict[tuple, dict] = {}
    for vals in itertools.product(*GRID.values()):
        combo = dict(zip(keys, vals))
        tr = [c["trades"][key] for c in select(past, combo, cooldown)
              if key in c["trades"] and c["trades"][key]["status"] == "CLOSED"]
        m = metrics(tr)
        total[vals] = (m.get("expectancy_r") or 0) * m.get("n", 0)
        info[vals] = m
    best, best_s = None, -math.inf
    for vals, tot in total.items():
        m = info[vals]
        if m.get("n", 0) < min_trades or (m.get("profit_factor") or 0) < 1.0:
            continue
        nb = [total[tuple(n[k] for k in keys)] for n in neighbours(dict(zip(keys, vals)))]
        s = 0.5 * tot + 0.5 * (sum(nb) / len(nb))
        if s > best_s:
            best, best_s = vals, s
    if best is None:
        return None, {}
    return dict(zip(keys, best)), info[best]


def bootstrap_p_le0(r: list[float], n: int = 10_000) -> float | None:
    if len(r) < 5:
        return None
    random.seed(7)
    return sum(st.mean(random.choices(r, k=len(r))) <= 0 for _ in range(n)) / n


def summarise(trades: list[dict]) -> dict:
    closed = [t for t in trades if t and t["status"] == "CLOSED"]
    m = metrics(closed)
    r = [t["r"] for t in closed if t.get("r") is not None]
    if not r:
        return {"n": 0}
    eq = peak = dd = 0.0
    for x in sorted(closed, key=lambda t: t["exit_ms"]):
        eq += x["r"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    top3 = sorted(r, reverse=True)[3:]
    return {**m, "total_r": sum(r), "usd_at_20": sum(r) * RISK_USD, "max_dd_r": dd,
            "median_r": st.median(r), "p_le0": bootstrap_p_le0(r),
            "without_top3_r": st.mean(top3) if top3 else None,
            "avg_risk_usd": st.mean(t["risk_usd"] for t in closed), "net_usd_1000": sum(t["net"] for t in closed)}


async def main_async(a) -> int:
    base = load_config(a.config)
    d = copy.deepcopy(base.to_dict())
    d["exchange"]["rate_limit_weight"] = a.rate or d["backtest"]["rate_limit_weight"]
    apply_overrides(d, [f"{k}={v}" for k, v in LOOSEST.items()])
    cfg = Config(d, base.path)
    setup_logging(cfg.data_dir / "logs", cfg.app.log_level)
    clock = Clock(cfg.app.display_tz)
    rest = WeexRest(cfg, clock)
    db = Database(cfg.database.url.format(data_dir=cfg.data_dir.as_posix()))
    t0 = time.time()
    try:
        prep = await prepare(cfg, rest, db, clock, a.days, a.symbols, a.max_symbols, [])
    finally:
        await rest.close()
    inp, start, end = prep["inp"], prep["start"], prep["end"]
    _say(f"Harvest: one pass of the live code over {prep['days']} days at the loosest settings...")
    bt = Backtester(cfg, inp, prep["disabled"])
    res = bt.run(start, end, lambda T, n, u: _say(f"  {clock.fmt(T, with_date=True)[:10]}: {n} candidates, universe {u}"))
    _say(f"{len(res['signals'])} candidates. Re-simulating each under {len(model_keys())} models...")
    cands = []
    for i, sig in enumerate(res["signals"]):
        tr = {}
        for lev in LEVERAGES:
            for pol, t in resimulate(bt, sig, lev, end, inp).items():
                tr[f"{lev}x-{pol}"] = t
        f = sig["features"]
        cands.append({"id": sig["signal_id"], "symbol": sig["symbol"], "setup": sig["setup"],
                      "T": int(sig["bar_close_ms"]), "score": int(sig["score"]), "rvol_5m": f.get("rvol_5m"),
                      "rvol_15m": f.get("rvol_15m"), "ret_1h": f.get("ret_1h"), "trades": tr})
        if (i + 1) % 100 == 0:
            _say(f"  {i + 1}/{len(res['signals'])}")
    cands.sort(key=lambda c: (c["T"], -c["score"]))

    out_dir = cfg.data_dir / cfg.backtest.report_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    (out_dir / f"study_candidates_{prep['days']}d.json").write_text(json.dumps(clean_json(
        {"cands": cands, "start": start, "end": end})), encoding="utf-8")

    cooldown = int(base.signals.symbol_cooldown_min) * 60_000
    fold_ms = a.fold_days * DAY
    folds = []
    t = end - a.folds * fold_ms
    while t < end:
        folds.append((t, min(t + fold_ms, end)))
        t += fold_ms
    current = {k: base.to_dict()[k.split(".")[0]][k.split(".")[1]] for k in GRID}

    results = {}
    for key in model_keys():
        wf_trades, fold_rows = [], []
        for f0, f1 in folds:
            combo, ins = pick_combo(cands, key, f0, cooldown, a.min_trades)
            use = combo or current
            window = [c for c in cands if f0 - cooldown <= c["T"] < f1]
            chosen = [c for c in select(window, use, cooldown) if c["T"] >= f0]
            tr = [c["trades"].get(key) for c in chosen]
            wf_trades += [x for x in tr if x]
            fold_rows.append({"from": f0, "to": f1, "combo": use, "tuned": combo is not None,
                              "in_sample": ins, "test": summarise(tr)})
        fixed = [c["trades"].get(key) for c in select(cands, current, cooldown)]
        results[key] = {"walk_forward": summarise(wf_trades), "folds": fold_rows,
                        "fixed_current_full": summarise(fixed),
                        "fixed_current_test": summarise([c["trades"].get(key) for c in select(cands, current, cooldown)
                                                         if c["T"] >= folds[0][0]])}
        w = results[key]["walk_forward"]
        _say(f"  {key:7s} walk-forward: {w.get('n', 0):3d} trades, "
             f"{(w.get('expectancy_r') or 0):+.3f}R/trade, total {w.get('total_r', 0):+6.1f}R")

    html_path = out_dir / f"study_{stamp}_{prep['days']}d.html"
    html_path.write_text(build_html(results, folds, prep, clock, cfg, time.time() - t0), encoding="utf-8")
    (out_dir / f"study_{stamp}_{prep['days']}d.json").write_text(json.dumps(clean_json(results)), encoding="utf-8")
    _say(f"Report: {html_path}  ({time.time() - t0:.0f}s)")
    return 0


def _r(x, d=2):
    return "–" if x is None else f"{x:+.{d}f}R"


def _usd(x):
    return "–" if x is None else ("-$" if x < 0 else "+$") + f"{abs(x):,.0f}"


def _pct(x):
    return "–" if x is None else f"{x * 100:.0f}%"


def build_html(results, folds, prep, clock, cfg, runtime) -> str:
    E = html.escape
    rows = []
    ranked = sorted(results.items(), key=lambda kv: kv[1]["walk_forward"].get("total_r", -1e9), reverse=True)
    for key, r in ranked:
        w = r["walk_forward"]
        fx = r["fixed_current_test"]
        cls = "pos" if (w.get("total_r") or 0) > 0 else "neg"
        pf = w.get("profit_factor")
        rows.append(
            f"<tr><td class=l><b>{E(key.replace('-', ' · Policy '))}</b></td><td>{w.get('n', 0)}</td>"
            f"<td>{_pct(w.get('win_rate'))}</td><td class={cls}>{_r(w.get('expectancy_r'))}</td>"
            f"<td class={cls}>{_r(w.get('total_r'), 1)}</td><td class={cls}>{_usd(w.get('usd_at_20'))}</td>"
            f"<td>{'∞' if pf == math.inf else ('–' if pf is None else f'{pf:.2f}')}</td>"
            f"<td>{_r(w.get('max_dd_r'), 1).replace('+', '-')}</td><td>{_pct(w.get('p_le0'))}</td>"
            f"<td>{_r(w.get('without_top3_r'))}</td><td>{'–' if not w.get('avg_risk_usd') else f'${w['avg_risk_usd']:.0f}'}</td>"
            f"<td class=muted>{fx.get('n', 0)} · {_r(fx.get('expectancy_r'))}</td></tr>")
    fold_html = ""
    for key, r in ranked:
        fr = "".join(
            f"<tr><td class=l>{E(clock.fmt(f['from'], True)[:10])} → {E(clock.fmt(f['to'], True)[:10])}</td>"
            f"<td class=l>{'tuned' if f['tuned'] else 'no eligible setting: current used'}</td>"
            f"<td class=l>{E(', '.join(f'{k.split(chr(46))[1]}={v}' for k, v in f['combo'].items()))}</td>"
            f"<td>{f['test'].get('n', 0)}</td><td>{_r(f['test'].get('expectancy_r'))}</td>"
            f"<td>{_r(f['test'].get('total_r'), 1)}</td></tr>" for f in r["folds"])
        fold_html += (f"<h3>{E(key)}</h3><div class=wrap><table><thead><tr><th class=l>Test window</th><th class=l>Settings</th>"
                      f"<th class=l>Thresholds chosen on data before the window</th><th>Trades</th><th>Avg R</th>"
                      f"<th>Total R</th></tr></thead><tbody>{fr}</tbody></table></div>")
    tz_period = f"{clock.fmt(prep['start'], True)[:10]} → {clock.fmt(prep['end'], True)[:10]}"
    test_period = f"{clock.fmt(folds[0][0], True)[:10]} → {clock.fmt(folds[-1][1], True)[:10]}"
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Walk-forward model study · Ignition &amp; Coil</title><style>{CSS}</style></head><body><main>
<h1>Walk-forward study: leverage × stop policy</h1>
<div class=muted>History {E(tz_period)} · test windows {E(test_period)} ({len(folds)} × {int((folds[0][1] - folds[0][0]) / DAY)} days) ·
{len(prep['inp'].bars) - 1} coins · runtime {runtime / 60:.0f} min</div>
<div class="card warn"><h2>How to read this</h2><ul>
<li><b>Every trade below was out-of-sample:</b> thresholds were chosen only on data before each test window, then applied to it.</li>
<li><b>R</b> = profit per unit of risk. <b>$ at $20 risk</b> scales every trade to risk exactly $20, the fair comparison across leverages
(lower leverage = wider liquidation-buffered stop = more $ at risk per $1,000 position).</li>
<li><b>P(edge ≤ 0)</b> = bootstrap probability that the true average is zero or negative given these trades. Below ~5% starts to be convincing.</li>
<li>Same limitations as every backtest: open-interest conditions off (no OI history), no spread/depth filter, only coins listed today,
instant entry at the signal bar's close.</li></ul></div>
<div class=card><h2>Ranking by out-of-sample total R</h2><div class=wrap><table><thead><tr>
<th class=l>Model</th><th>Trades</th><th>Win</th><th>Avg R</th><th>Total R</th><th>$ at $20 risk</th><th>PF</th><th>Max DD</th>
<th>P(edge ≤ 0)</th><th>Avg R w/o top 3</th><th>Avg $ risk per $1,000</th><th>Current settings (same windows)</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table></div></div>
<div class=card><h2>Per test window</h2>{fold_html}</div>
</main></body></html>"""


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Walk-forward leverage x stop-policy study")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--fold-days", type=int, default=60)
    ap.add_argument("--min-trades", type=int, default=20, help="minimum in-sample trades for a setting to be eligible")
    ap.add_argument("--rate", type=int, default=None, help="API weight per 10 s for the download")
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--max-symbols", type=int, default=None)
    ap.add_argument("--config", default=None)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
