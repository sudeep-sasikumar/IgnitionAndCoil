"""Funding settlements per symbol, for P&L (spec §2: include funding for positions held
through a settlement). Sources: live observation of premiumIndex rolling over, plus WEEX
funding-rate history (REST) for anything older. Settlements happen at each symbol's own
interval (1h / 4h / 8h on WEEX)."""
from __future__ import annotations

import bisect
import logging

log = logging.getLogger("funding")


def funding_usd(settlements: list[tuple[int, float, float]], entry_ms: int, qty_at,
                fallback_price: float) -> float:
    """Signed $ funding for a LONG: longs pay positive rates. settlements = [(ts, rate, mark)];
    qty_at(ts) -> base quantity open at that settlement. fallback_price is used when the
    settlement record has no mark price."""
    total = 0.0
    for ts, rate, mark in settlements:
        if ts <= entry_ms:
            continue
        q = qty_at(ts)
        if q > 0:
            total -= rate * q * (mark if mark > 0 else fallback_price)
    return total


def qty_timeline(qty: float, legs: list[dict]):
    """qty_at(ts) from the original qty and closing legs [{ts, qty}] (a leg at ts closes before it)."""
    closes = sorted((int(l["ts"]), float(l["qty"])) for l in legs)

    def qty_at(ts: int) -> float:
        return qty - sum(q for t, q in closes if t <= ts)
    return qty_at


class FundingBook:
    def __init__(self, rest):
        self.rest = rest
        self._s: dict[str, dict[int, tuple[float, float]]] = {}
        self._fetched: dict[str, tuple[int, int]] = {}
        self._next: dict[str, int] = {}

    def add(self, sym: str, ts: int, rate: float, mark: float) -> None:
        self._s.setdefault(sym, {})[int(ts)] = (float(rate), float(mark))

    def observe(self, premium: dict) -> None:
        """Call after each premiumIndex poll. When nextFundingTime advances, the previous one
        just settled at lastFundingRate."""
        for sym, p in premium.items():
            prev = self._next.get(sym)
            if prev is not None and p.next_funding_ms > prev:
                self.add(sym, prev, p.last_funding, p.mark)
            self._next[sym] = p.next_funding_ms

    async def settlements(self, sym: str, start_ms: int, end_ms: int) -> list[tuple[int, float, float]]:
        """Settlements in (start_ms, end_ms], fetching history from WEEX when not yet known."""
        have = self._fetched.get(sym)
        if have is None or start_ms < have[0] or end_ms > have[1]:
            try:
                rows = await self.rest.funding_history(sym, start_ms, end_ms)
                for r in rows:
                    self.add(sym, int(r["fundingTime"]), float(r["fundingRate"]), float(r.get("markPrice") or 0))
                lo = min(start_ms, have[0]) if have else start_ms
                hi = max(end_ms, have[1]) if have else end_ms
                self._fetched[sym] = (lo, hi)
            except Exception as e:  # noqa: BLE001 - fall back to what was observed live
                log.warning("funding history %s failed: %s", sym, e)
        s = self._s.get(sym, {})
        keys = sorted(s)
        i, j = bisect.bisect_right(keys, start_ms), bisect.bisect_right(keys, end_ms)
        return [(k, *s[k]) for k in keys[i:j]]
