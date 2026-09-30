"""Historical data for backtests: klines (5m/15m/1h/4h) and funding history, cached on disk.

Each timeframe is fetched with the same warm-up the live ring buffers hold (bars.keep), so
indicators in the backtest see exactly the windows the live engine sees. Re-runs only fetch
what is missing. OI history is NOT available from WEEX (see M0) - the backtester uses our own
stored snapshots when they cover the period.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import numpy as np

from core.clock import TF_MS, floor_tf
from data.bars import BarArrays

log = logging.getLogger("backtest.data")

TFS = ("5m", "15m", "1h", "4h")
FIELDS = ("t", "o", "h", "l", "c", "v", "qv", "tbv", "tbqv", "tc")


def merge_arrays(a: BarArrays | None, b: BarArrays) -> BarArrays:
    if a is None or not len(a):
        return b
    if not len(b):
        return a
    cat = {f: np.concatenate([getattr(a, f), getattr(b, f)]) for f in FIELDS}
    # de-duplicate by open time, the later download wins
    order = np.argsort(cat["t"], kind="stable")
    t_sorted = cat["t"][order]
    last = np.r_[t_sorted[1:] != t_sorted[:-1], True]
    idx = order[last]
    return BarArrays(**{f: cat[f][idx] for f in FIELDS})



def _atomic_write(path: Path, write) -> None:
    """Write to a temp file, then swap it in: backtests running side by side never read a
    half-written cache file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        write(f)
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # Windows: another run has the file open for a moment
            time.sleep(0.1 * (attempt + 1))
    os.replace(tmp, path)

class HistoryCache:
    def __init__(self, cfg, rest, now_ms: int):
        self.cfg, self.rest, self.now_ms = cfg, rest, now_ms
        self.dir = cfg.data_dir / cfg.backtest.cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, sym: str, tf: str) -> Path:
        return self.dir / f"{sym}_{tf}.npz"

    def load(self, sym: str, tf: str) -> tuple[BarArrays | None, int, int]:
        p = self._path(sym, tf)
        if not p.exists():
            return None, 0, 0
        with np.load(p) as z:          # closed at once, so another run can replace the file
            arr = BarArrays(**{f: z[f] for f in FIELDS})
            return arr, int(z["req_from"]), int(z["req_to"])

    def _save(self, sym: str, tf: str, arr: BarArrays, req_from: int, req_to: int) -> None:
        _atomic_write(self._path(sym, tf), lambda f: np.savez(f, req_from=req_from, req_to=req_to,
                                                              **{k: getattr(arr, k) for k in FIELDS}))

    async def bars(self, sym: str, tf: str, start_ms: int, end_ms: int) -> BarArrays:
        """Closed bars with open time in [start_ms, end_ms], downloading what the cache lacks."""
        step = TF_MS[tf]
        start = floor_tf(start_ms, tf)
        end = min(floor_tf(end_ms, tf), floor_tf(self.now_ms, tf) - step)
        have, rf, rt = self.load(sym, tf)
        ranges = []
        if have is None:
            ranges.append((start, end))
        else:
            if start < rf:
                ranges.append((start, rf - step))
            if end > rt:
                ranges.append((rt + step, end))
        for a, b in ranges:
            if a > b:
                continue
            got = BarArrays.from_bars(await self.rest.history_klines(sym, tf, a, b))
            have = merge_arrays(have, got)
            rf = min(rf, a) if rf else a
            rt = max(rt, b)
        if have is None:
            have = BarArrays.from_bars([])
        if ranges:
            self._save(sym, tf, have, rf or start, rt or end)
        lo = int(np.searchsorted(have.t, start))
        hi = int(np.searchsorted(have.t, end, side="right"))
        return BarArrays(**{f: getattr(have, f)[lo:hi] for f in FIELDS})

    async def funding(self, sym: str, start_ms: int, end_ms: int) -> list[tuple[int, float, float]]:
        """Settlements (ts, rate, mark) in [start, end]; WEEX keeps 365 days of funding history."""
        p = self.dir / f"{sym}_funding.json"
        data = json.loads(p.read_text()) if p.exists() else {"from": 0, "to": 0, "rows": []}
        start = max(start_ms, self.now_ms - 364 * 86_400_000)
        need = []
        if not data["rows"] and not data["to"]:
            need.append((start, end_ms))
        else:
            if start < data["from"]:
                need.append((start, data["from"]))
            if end_ms > data["to"]:
                need.append((data["to"], end_ms))
        rows = {int(r[0]): r for r in data["rows"]}
        for a, b in need:
            if a >= b:
                continue
            for r in await self.rest.funding_history(sym, a, b):
                rows[int(r["fundingTime"])] = [int(r["fundingTime"]), float(r["fundingRate"]),
                                               float(r.get("markPrice") or 0)]
        if need:
            data = {"from": min(data["from"] or start, start), "to": max(data["to"], end_ms),
                    "rows": sorted(rows.values())}
            _atomic_write(p, lambda f: f.write(json.dumps(data).encode()))
        return [(int(t), float(r), float(m)) for t, r, m in sorted(rows.values()) if start_ms <= t <= end_ms]


def aggregate(b5: BarArrays, tf: str) -> BarArrays:
    """Build a higher timeframe from 5m bars. Verified: WEEX 15m klines equal the aggregate of
    their three 5m klines exactly (8,977 bars x 4 symbols, only float rounding differences)."""
    if not len(b5):
        return b5
    step = TF_MS[tf]
    key = b5.t - b5.t % step
    starts = np.r_[0, np.flatnonzero(np.diff(key)) + 1]
    ends = np.r_[starts[1:], len(key)]
    red = lambda arr, fn: fn.reduceat(arr, starts)
    return BarArrays(t=key[starts], o=b5.o[starts], h=red(b5.h, np.maximum), l=red(b5.l, np.minimum),
                     c=b5.c[ends - 1], v=red(b5.v, np.add), qv=red(b5.qv, np.add), tbv=red(b5.tbv, np.add),
                     tbqv=red(b5.tbqv, np.add), tc=key[starts] + step)


def warmup_ms(cfg, tf: str) -> int:
    return int(cfg.bars.keep[tf]) * TF_MS[tf]
