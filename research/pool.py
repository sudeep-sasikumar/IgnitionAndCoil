"""Backtest coin pool WITHOUT hindsight: coins that could have entered the live universe at the
time, instead of "today's top 40 by volume" (which favours coins that became big by rallying).

    .venv\\Scripts\\python.exe -m research.pool --period y2025:"2025-09-30 01:20" --period y2026:"2026-09-30 01:20"

For every WEEX USDT perpetual listed today: daily klines (1 request each). A coin enters a
period's pool if on some two consecutive UTC days its quote volume summed to at least
universe.min_quote_volume_24h - a safe superset of "a rolling 24h window reached the threshold"
(a 24h window spans at most two calendar days). The backtester then applies the exact live
universe rules bar by bar (rolling 24h volume, ATR, wicks, listing age, max_symbols).
Limitation: WEEX lists only CURRENT contracts, so coins delisted meanwhile cannot be included.
Writes <data_dir>/research/pool.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.clock import Clock, parse_local  # noqa: E402
from core.config import load_config  # noqa: E402
from exchange.weex_rest import WeexRest  # noqa: E402

DAY = 86_400_000


async def daily_volumes(cfg) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    rest = WeexRest(cfg, Clock(cfg.app.display_tz))
    try:
        info = await rest.exchange_info()
        syms = sorted(s["symbol"] for s in info["symbols"]
                      if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT")
        sem = asyncio.Semaphore(8)
        out = {}
        skipped: list[str] = []

        async def one(sym):
            async with sem:
                try:
                    bars = await rest.klines(sym, "1d", 1000)
                except Exception:  # noqa: BLE001 - listed but not queryable (WEEX answers "symbol invalid")
                    skipped.append(sym)
                    return
                if bars:
                    out[sym] = (np.array([b.t for b in bars], np.int64), np.array([b.qv for b in bars], float))
        await asyncio.gather(*(one(s) for s in syms))
        if skipped:
            print(f"  {len(skipped)} listed perps have no klines on WEEX (skipped): {', '.join(sorted(skipped))}")
        return out
    finally:
        await rest.close()


def pool_for(vols: dict, start: int, end: int, threshold: float, exclude: set[str]) -> list[str]:
    out = []
    for sym, (t, qv) in vols.items():
        if sym in exclude:
            continue
        m = (t >= start - DAY) & (t <= end)
        q = qv[m]
        if len(q) and (np.max(q[:-1] + q[1:]) if len(q) > 1 else q[0]) >= threshold:
            out.append(sym)
    return sorted(out)


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Hindsight-free backtest coin pool")
    ap.add_argument("--period", action="append", required=True, metavar="TAG:'YYYY-MM-DD HH:MM'",
                    help="period tag and its end (London time); each period is 365 days")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    cfg = load_config(a.config)
    u = cfg.universe
    vols = asyncio.run(daily_volumes(cfg))
    stables = {s.upper() for s in u.stablecoin_bases}
    exclude = set(u.majors) | {s for s in vols if s[:-4].upper() in stables}
    res = {"threshold": float(u.min_quote_volume_24h), "listed_now": len(vols), "periods": {}}
    old = Path(cfg.data_dir).parent / "var" / "syms_365_pool.txt"
    today40 = set(old.read_text().strip().split(",")) if old.exists() else set()
    for spec in a.period:
        tag, end_s = spec.split(":", 1)
        end = parse_local(end_s.strip().strip('"'), cfg.app.display_tz)
        start = end - a.days * DAY
        pool = pool_for(vols, start, end, float(u.min_quote_volume_24h), exclude)
        res["periods"][tag] = {"end": end_s, "symbols": pool}
        print(f"{tag}: {len(pool)} coins could have qualified (vs {len(today40)} in the 'today's top 40' pool); "
              f"{len(today40 & set(pool))} of those 40 are in it, {len(set(pool) - today40)} new")
    p = Path(cfg.data_dir) / "research" / "pool.json"
    p.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(f"{len(vols)} perps listed today. Saved: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
