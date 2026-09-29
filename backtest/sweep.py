"""Threshold sweep: find settings that give more signals without giving up quality.

    .venv\\Scripts\\python.exe -m backtest.sweep --days 90 --holdout-days 30

How it works (fast and honest):
1. ONE full backtest with the live code at the LOOSEST values of the grid, score cut-off 0 and
   no cooldown. Every bar that would pass under some grid setting becomes a candidate, with its
   features, score and simulated L/S trade outcome. (Outcomes don't depend on these thresholds:
   the plan and exits only depend on price, stops and resistance.)
2. Each grid combination is scored by filtering those candidates with its thresholds and
   re-applying the per-symbol cooldown in time order - milliseconds per combination.
3. Settings are picked on the first part of the period (in-sample) using a neighbourhood-smoothed
   objective, then checked on the last `holdout_days` the choice never saw (out-of-sample).
4. Confirm the pick with a normal full backtest (exact): python -m backtest --set ...
The Coil candidates are approximate in step 1 (WATCH consumption differs without a cooldown);
the confirmation run is exact.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import itertools
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.__main__ import _say, apply_overrides, prepare  # noqa: E402
from backtest.engine import Backtester  # noqa: E402
from core.clock import Clock  # noqa: E402
from core.config import Config, load_config  # noqa: E402
from core.logs import setup_logging  # noqa: E402
from data.db import Database, clean_json  # noqa: E402
from exchange.weex_rest import WeexRest  # noqa: E402
from stats.metrics import metrics  # noqa: E402

DAY = 86_400_000

GRID = {
    "ignition.min_rvol_5m": [3.0, 2.5, 2.0, 1.5],
    "ignition.min_rvol_15m": [2.0, 1.5, 1.2],
    "ignition.min_ret_1h": [1.0, 0.75, 0.5],
    "ignition.max_ret_1h": [5.0, 6.5, 8.0],
    "coil.min_rvol_15m": [2.5, 2.0, 1.5],
    "score.min_entry": [70, 65, 60, 55, 50],
}
LOOSEST = {"ignition.min_rvol_5m": 1.5, "ignition.min_rvol_15m": 1.2, "ignition.min_ret_1h": 0.5,
           "ignition.max_ret_1h": 8.0, "coil.min_rvol_15m": 1.5, "score.min_entry": 0,
           "signals.symbol_cooldown_min": 0}


def harvest_candidates(result: dict) -> list[dict]:
    by_sig: dict[str, dict] = {}
    for t in result["trades"]:
        by_sig.setdefault(t["signal_id"], {})[t["policy"]] = t
    out = []
    for s in result["signals"]:
        f = s["features"]
        out.append({"id": s["signal_id"], "symbol": s["symbol"], "setup": s["setup"], "T": int(s["bar_close_ms"]),
                    "score": int(s["score"]), "rvol_5m": f.get("rvol_5m"), "rvol_15m": f.get("rvol_15m"),
                    "ret_1h": f.get("ret_1h"), "trades": by_sig.get(s["signal_id"], {})})
    out.sort(key=lambda c: (c["T"], -c["score"]))
    return out


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def select(cands: list[dict], combo: dict, cooldown_ms: int) -> list[dict]:
    """Candidates that would have fired under `combo`, with the live per-symbol cooldown."""
    last: dict[str, int] = {}
    chosen = []
    for c in cands:
        if c["score"] < combo["score.min_entry"]:
            continue
        if c["setup"] == "IGNITION":
            ok = (_ok(c["rvol_5m"]) and c["rvol_5m"] >= combo["ignition.min_rvol_5m"]
                  and _ok(c["rvol_15m"]) and c["rvol_15m"] >= combo["ignition.min_rvol_15m"]
                  and _ok(c["ret_1h"]) and combo["ignition.min_ret_1h"] <= c["ret_1h"] <= combo["ignition.max_ret_1h"])
        else:
            ok = _ok(c["rvol_15m"]) and c["rvol_15m"] >= combo["coil.min_rvol_15m"]
        if not ok or c["T"] - last.get(c["symbol"], -10 ** 15) < cooldown_ms:
            continue
        last[c["symbol"]] = c["T"]
        chosen.append(c)
    return chosen


def score_combo(cands, combo, cooldown_ms, split_ms, policy="L") -> dict:
    chosen = select(cands, combo, cooldown_ms)
    trades = [c["trades"][policy] for c in chosen if policy in c["trades"] and c["trades"][policy]["status"] == "CLOSED"]
    ins = [t for t in trades if t["entry_ms"] < split_ms]
    oos = [t for t in trades if t["entry_ms"] >= split_ms]
    m_in, m_out = metrics(ins), metrics(oos)
    return {"combo": combo, "signals": len(chosen), "in": m_in, "out": m_out,
            "in_total_r": (m_in.get("expectancy_r") or 0) * m_in.get("n", 0),
            "out_total_r": (m_out.get("expectancy_r") or 0) * m_out.get("n", 0)}


def neighbours(combo: dict) -> list[dict]:
    out = []
    for k, vals in GRID.items():
        i = vals.index(combo[k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                out.append({**combo, k: vals[j]})
    return out


def sweep(cands, cooldown_ms, split_ms) -> list[dict]:
    keys = list(GRID)
    rows = {}
    for vals in itertools.product(*GRID.values()):
        combo = dict(zip(keys, vals))
        rows[vals] = score_combo(cands, combo, cooldown_ms, split_ms)
    for vals, r in rows.items():
        nb = [rows[tuple(n[k] for k in keys)]["in_total_r"] for n in neighbours(r["combo"])]
        # robustness: the pick should sit in a good region, not on a lucky spike
        r["smoothed_in_r"] = 0.5 * r["in_total_r"] + 0.5 * (sum(nb) / len(nb) if nb else 0)
    return list(rows.values())


def fmt_row(r: dict) -> str:
    c = r["combo"]
    mi, mo = r["in"], r["out"]
    return (f"rvol5≥{c['ignition.min_rvol_5m']:<3} rvol15≥{c['ignition.min_rvol_15m']:<3} "
            f"ret1h {c['ignition.min_ret_1h']}-{c['ignition.max_ret_1h']:<4} coil rvol15≥{c['coil.min_rvol_15m']:<3} "
            f"score≥{c['score.min_entry']:<3}| in: {mi.get('n', 0):3d} tr, win {100 * (mi.get('win_rate') or 0):3.0f}%, "
            f"{(mi.get('expectancy_r') or 0):+.2f}R, total {r['in_total_r']:+6.1f}R (smoothed {r['smoothed_in_r']:+6.1f}) "
            f"| out: {mo.get('n', 0):3d} tr, win {100 * (mo.get('win_rate') or 0):3.0f}%, {(mo.get('expectancy_r') or 0):+.2f}R, "
            f"total {r['out_total_r']:+5.1f}R")


async def main_async(a) -> int:
    base = load_config(a.config)
    d = copy.deepcopy(base.to_dict())
    d["exchange"]["rate_limit_weight"] = d["backtest"]["rate_limit_weight"]
    apply_overrides(d, [f"{k}={v}" for k, v in LOOSEST.items()])
    cfg = Config(d, base.path)
    setup_logging(cfg.data_dir / "logs", cfg.app.log_level)
    clock = Clock(cfg.app.display_tz)
    rest = WeexRest(cfg, clock)
    db = Database(cfg.database.url.format(data_dir=cfg.data_dir.as_posix()))
    t0 = time.time()
    out_dir = cfg.data_dir / cfg.backtest.report_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    cand_path = out_dir / f"sweep_candidates_{a.days}d.json"
    if a.reuse and cand_path.exists():
        saved = json.loads(cand_path.read_text(encoding="utf-8"))
        cands, prep = saved["cands"], saved["prep"]
        prep["disabled"] = set(prep["disabled"])
        _say(f"Reusing {len(cands)} saved candidates from {cand_path.name}")
    else:
        try:
            prep = await prepare(cfg, rest, db, clock, a.days, a.symbols, a.max_symbols, [])
        finally:
            await rest.close()
        _say(f"Harvesting candidates: one pass of the live code over {prep['days']} days at the loosest settings...")
        res = Backtester(cfg, prep["inp"], prep["disabled"]).run(
            prep["start"], prep["end"],
            lambda T, n, u: _say(f"  {clock.fmt(T, with_date=True)[:10]}: {n} candidates, universe {u}"))
        cands = harvest_candidates(res)
        prep = {k: prep[k] for k in ("start", "end", "days")} | {"disabled": sorted(prep["disabled"])}
        cand_path.write_text(json.dumps(clean_json({"cands": cands, "prep": prep})), encoding="utf-8")
        prep["disabled"] = set(prep["disabled"])
    await rest.close()
    start, end = prep["start"], prep["end"]
    split = end - a.holdout_days * DAY
    cooldown = int(base.signals.symbol_cooldown_min) * 60_000
    _say(f"{len(cands)} candidates. Scoring {math.prod(len(v) for v in GRID.values())} combinations...")
    rows = sweep(cands, cooldown, split)

    current = {k: base.to_dict()[k.split(".")[0]][k.split(".")[1]] for k in GRID}
    baseline = score_combo(cands, current, cooldown, split)
    baseline["smoothed_in_r"] = baseline["in_total_r"]
    base_n = baseline["in"].get("n", 0)
    eligible = [r for r in rows if r["in"].get("n", 0) >= max(2 * base_n, base_n + 10)
                and (r["in"].get("profit_factor") or 0) >= 1.0]
    eligible.sort(key=lambda r: r["smoothed_in_r"], reverse=True)
    pick = eligible[0] if eligible else None

    stamp = time.strftime("%Y%m%d_%H%M")
    csv_path = out_dir / f"sweep_{stamp}_{prep['days']}d.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([*GRID, "signals", "in_n", "in_win", "in_exp_r", "in_pf", "in_total_r", "smoothed_in_r",
                    "out_n", "out_win", "out_exp_r", "out_pf", "out_total_r"])
        for r in sorted(rows, key=lambda r: r["smoothed_in_r"], reverse=True):
            mi, mo = r["in"], r["out"]
            w.writerow([*r["combo"].values(), r["signals"], mi.get("n", 0), mi.get("win_rate"), mi.get("expectancy_r"),
                        mi.get("profit_factor"), r["in_total_r"], r["smoothed_in_r"], mo.get("n", 0),
                        mo.get("win_rate"), mo.get("expectancy_r"), mo.get("profit_factor"), r["out_total_r"]])

    lines = [f"Period {clock.fmt(start, True)[:10]} → {clock.fmt(end, True)[:10]}; in-sample until "
             f"{clock.fmt(split, True)[:10]}, out-of-sample = last {a.holdout_days} days. Policy L. "
             f"OI conditions {'disabled' if 'oi' in prep['disabled'] else 'enabled'}.",
             "CURRENT: " + fmt_row(baseline), "", "Top 12 by smoothed in-sample total R (≥ 2x signals, PF ≥ 1):"]
    lines += [fmt_row(r) for r in eligible[:12]]
    lines += ["", "PICK: " + (fmt_row(pick) if pick else "none met the criteria")]
    text = "\n".join(lines)
    _say(text)
    (out_dir / f"sweep_{stamp}_{prep['days']}d.txt").write_text(text, encoding="utf-8")
    if pick:
        sets = " ".join(f"--set {k}={v}" for k, v in pick["combo"].items())
        _say(f"\nConfirm exactly with:\n  .venv\\Scripts\\python.exe -m backtest --days {prep['days']} --tag confirm {sets}")
    _say(f"All {len(rows)} combinations: {csv_path}  ({time.time() - t0:.0f}s)")
    return 0


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Threshold sweep with an out-of-sample check")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--holdout-days", type=int, default=30)
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--max-symbols", type=int, default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--reuse", action="store_true", help="re-score saved candidates (skip the harvest pass)")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
