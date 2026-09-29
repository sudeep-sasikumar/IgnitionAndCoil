"""Open-interest history built from our own 60 s polls (WEEX has no OI-history endpoint)."""
from __future__ import annotations

import asyncio
import bisect
import logging

log = logging.getLogger("oi")

KEEP_MS = 5 * 3_600_000  # in memory: a little over the 4h lookback


def oi_change_pct(ts: list[int], oi: list[float], at_ms: int, minutes: int, tol_ms: int) -> float | None:
    """% change of OI between the latest snapshot at/before at_ms and the one nearest
    (at_ms - minutes). Returns None when either end has no snapshot within tolerance."""
    i_now = bisect.bisect_right(ts, at_ms) - 1
    if i_now < 0 or at_ms - ts[i_now] > tol_ms:
        return None
    target = at_ms - minutes * 60_000
    j = bisect.bisect_left(ts, target)
    best = None
    for k in (j - 1, j):
        if 0 <= k < i_now and abs(ts[k] - target) <= tol_ms:
            if best is None or abs(ts[k] - target) < abs(ts[best] - target):
                best = k
    if best is None or oi[best] <= 0:
        return None
    return (oi[i_now] / oi[best] - 1.0) * 100.0


class OITracker:
    def __init__(self, cfg, rest, db):
        self.rest, self.db = rest, db
        self.tol_ms = cfg.open_interest.match_tolerance_s * 1000
        self._ts: dict[str, list[int]] = {}
        self._oi: dict[str, list[float]] = {}
        self.last_poll_ms = 0

    def load_history(self, now_ms: int) -> None:
        rows = self.db.load_oi_since(now_ms - KEEP_MS)
        for sym, ts, oi in rows:
            self._append(sym, ts, oi)
        log.info("loaded %d OI snapshots from DB", len(rows))

    def _append(self, sym: str, ts: int, oi: float) -> None:
        tsl, oil = self._ts.setdefault(sym, []), self._oi.setdefault(sym, [])
        if tsl and ts <= tsl[-1]:
            return
        tsl.append(ts)
        oil.append(oi)
        cut = bisect.bisect_left(tsl, ts - KEEP_MS)
        if cut:
            del tsl[:cut], oil[:cut]

    async def poll(self, symbols: list[str]) -> None:
        async def one(sym: str):
            try:
                d = await self.rest.open_interest(sym)
                return sym, int(d["time"]), float(d["openInterest"])
            except Exception as e:  # noqa: BLE001 - one symbol failing must not stop the rest
                log.debug("OI %s failed: %s", sym, e)
                return None

        res = await asyncio.gather(*(one(s) for s in symbols))
        rows = [r for r in res if r]
        for sym, ts, oi in rows:
            self._append(sym, ts, oi)
        self.db.add_oi(rows)
        if rows:
            self.last_poll_ms = max(r[1] for r in rows)

    def change(self, sym: str, minutes: int, at_ms: int) -> float | None:
        ts = self._ts.get(sym)
        if not ts:
            return None
        return oi_change_pct(ts, self._oi[sym], at_ms, minutes, self.tol_ms)

    def history_minutes(self, sym: str) -> float:
        ts = self._ts.get(sym)
        return (ts[-1] - ts[0]) / 60_000 if ts and len(ts) > 1 else 0.0
