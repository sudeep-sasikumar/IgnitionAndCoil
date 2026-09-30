"""Candidate systems: rules suggested by the miner, replayed as real trading systems on the history
dataset's breakout bars with the live alert rules - per-coin cooldown (set even if the alert is
suppressed), hourly cap, "Policy S must fit" - and the exact simulated Policy S trades.

    .venv\\Scripts\\python.exe -m research.candidates --history y2025,y2026

The current system is rebuilt the same way first and must match the recorded signals.
Rules found on y2025 are out-of-sample on y2026.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_config  # noqa: E402
from research.mine import active_conds, load_history  # noqa: E402


def gate(rows: pd.DataFrame, cooldown_ms: int, cap: int) -> pd.DataFrame:
    """Live rules over the candidate rows (already filtered). Returns the SENT rows."""
    fired = []
    for sym, g in rows.sort_values("ts").groupby("symbol"):
        last = -10 ** 15
        for i, ts in zip(g.index, g["ts"].to_numpy()):
            if ts - last >= cooldown_ms:
                fired.append(i)
                last = ts
    f = rows.loc[fired].sort_values(["ts", "score", "symbol"], ascending=[True, False, True])
    sent, window = [], deque()
    for i, ts, ok in zip(f.index, f["ts"].to_numpy(), f["trade_r"].notna().to_numpy()):
        while window and window[0] <= ts - 3_600_000:
            window.popleft()
        if not ok or len(window) >= cap:        # Policy S n/a -> suppressed; hourly cap
            continue
        window.append(ts)
        sent.append(i)
    return rows.loc[sent]


def stats(s: pd.DataFrame) -> dict:
    r = s.sort_values("ts")["trade_r"].to_numpy(float)
    net = s["trade_net"].to_numpy(float)
    if not len(r):
        return {"n": 0}
    eq = np.r_[0, np.cumsum(r)]
    gw, gl = net[net > 0].sum(), -net[net < 0].sum()
    return {"n": int(len(r)), "R": round(float(r.sum()), 1), "avgR": round(float(r.mean()), 3),
            "win": round(float((r > 0).mean()), 2), "pf": round(float(gw / gl), 2) if gl > 0 else None,
            "net": round(float(net.sum())), "maxdd_R": round(float((np.maximum.accumulate(eq) - eq).max()), 1)}


def mask(df: pd.DataFrame, rule: list[tuple]) -> pd.Series:
    m = pd.Series(True, index=df.index)
    for f, op, v in rule:
        m &= (df[f] >= v) if op == ">=" else (df[f] <= v)
    return m


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Replay candidate rule systems")
    ap.add_argument("--history", default="y2025,y2026")
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    cfg = load_config(a.config)
    df = load_history(cfg, a.history.split(","))
    b = df[df["c_break"] == 1].copy()
    cd, cap, thr = int(cfg.signals.symbol_cooldown_min) * 60_000, int(cfg.signals.max_alerts_per_hour), int(cfg.score.min_entry)
    act = active_conds(cfg)

    def current(g):          # every active condition (no-data columns count as off, like the backtest)
        m = g["score"] >= thr
        for c in act:
            if (g[c] == -1).mean() < 0.95:
                m &= g[c] == 1
        return m

    systems = {
        "Current system": lambda g: current(g),
        "Current + avoid weak breaks (break_pct >= 0.42)": lambda g: current(g) & mask(g, [("break_pct", ">=", 0.4238)]),
        "Current + avoid score >= 82": lambda g: current(g) & mask(g, [("score", "<=", 81)]),
        "Current + headroom >= 4.4%": lambda g: current(g) & mask(g, [("headroom", ">=", 4.429)]),
        "Current + all three avoid rules": lambda g: current(g) & mask(g, [("break_pct", ">=", 0.4238), ("score", "<=", 81),
                                                                          ("headroom", ">=", 4.429)]),
        "Current + BTC up >= 0.5% in 45m": lambda g: current(g) & mask(g, [("btc_ret_45m", ">=", 0.496)]),
        "Current + rs_1h >= 4.75": lambda g: current(g) & mask(g, [("rs_1h", ">=", 4.749)]),
        "NEW burst: big candle (>=1.52 ATR) + 1h >= +3.8%": lambda g: mask(g, [("candle_atr", ">=", 1.517), ("ret_1h", ">=", 3.806)]),
        "NEW burst + current active filters": lambda g: current(g) & mask(g, [("candle_atr", ">=", 1.517), ("ret_1h", ">=", 3.806)]),
        "NEW burst + score >= 55": lambda g: mask(g, [("candle_atr", ">=", 1.517), ("ret_1h", ">=", 3.806), ("score", ">=", thr)]),
        "NEW burst + headroom >= 3%": lambda g: mask(g, [("candle_atr", ">=", 1.517), ("ret_1h", ">=", 3.806), ("headroom", ">=", 3.0)]),
        "NEW stretch: vwap >= +7.6% + rvol_5m >= 3.57": lambda g: mask(g, [("vwap_dist", ">=", 7.613), ("rvol_5m", ">=", 3.572)]),
        "NEW burst2: big candle + 4h >= +4.8%": lambda g: mask(g, [("candle_atr", ">=", 1.517), ("ret_4h", ">=", 4.766)]),
    }
    out = {}
    for name, fn in systems.items():
        out[name] = {}
        for p, g in b.groupby("period"):
            out[name][p] = stats(gate(g[fn(g)], cd, cap))
    # the rebuilt current system must match the signals the live code produced
    for p, g in df.groupby("period"):
        rec = g[(g["sent"] == 1) & g["trade_r"].notna()]
        print(f"check {p}: recorded sent {len(rec)} trades, sum R {rec['trade_r'].sum():+.4f} | rebuilt {out['Current system'][p]}")
    print()
    periods = sorted(b["period"].unique())
    for name, res in out.items():
        print(f"{name}")
        for p in periods:
            x = res[p]
            print(f"   {p}: " + (f"n={x['n']:4d} R={x['R']:+7.1f} avg={x['avgR']:+.3f} win={x['win']:.2f} PF={x['pf']} "
                                 f"net=${x['net']:+,} maxDD={x['maxdd_R']}R" if x["n"] else "no trades"))
    p = Path(cfg.data_dir) / "research" / "reports" / "candidates.json"
    p.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"Saved: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
