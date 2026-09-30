"""Whole-market recorder: every WEEX USDT perpetual, every 5 minutes - only what can NOT be
downloaded later (prices, volumes, taker flow, mark/index are all in WEEX's kline history):

  funding_fc   forecast funding rate, 8h-normalised, % (from the premiumIndex poll: 0 extra requests)
  oi           open interest (WEEX units; only changes matter)          1 request (w2) per coin
  spread       top-of-book spread, % of mid                             1 request (w1) per coin
  bid05/ask05  USD resting within 0.5% of mid, top-15 levels            (same request)

Storage: one file per UTC day, <data_dir>/research/market/market-YYYY-MM-DD.npz, holding
`ts` [bars], `symbols` [coins] and one float32 [bars x coins] matrix per field (NaN = no data),
compressed - about 1-2 MB a day for ~570 coins. Rewritten atomically after every bar, so a crash
loses at most one bar; a restart continues the day's file. Requests are paced
(research.market_rps) so the scanner always keeps most of WEEX's rate budget.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

log = logging.getLogger("research")
FIELDS = ("funding_fc", "oi", "spread", "bid05", "ask05")
M5 = 300_000


def book_top(bids: list, asks: list, band_pct: float = 0.5) -> tuple[float, float, float]:
    """(spread %, USD bids within band, USD asks within band) from depth levels [[price, qty], ...]."""
    b = [(float(p), float(q)) for p, q in bids]
    a = [(float(p), float(q)) for p, q in asks]
    if not b or not a:
        return (np.nan, np.nan, np.nan)
    bb, ba = max(p for p, _ in b), min(p for p, _ in a)
    mid = (bb + ba) / 2
    lo, hi = mid * (1 - band_pct / 100), mid * (1 + band_pct / 100)
    return ((ba - bb) / mid * 100, sum(p * q for p, q in b if p >= lo), sum(p * q for p, q in a if p <= hi))


class DayStore:
    """A day's [bars x coins] float32 matrices; coins can join mid-day."""

    def __init__(self, path: Path):
        self.path = path
        self.ts: list[int] = []
        self.symbols: list[str] = []
        self.cols: dict[str, np.ndarray] = {f: np.zeros((0, 0), np.float32) for f in FIELDS}
        if path.exists():
            with np.load(path, allow_pickle=False) as z:
                self.ts = [int(x) for x in z["ts"]]
                self.symbols = [str(x) for x in z["symbols"]]
                self.cols = {f: z[f].astype(np.float32) for f in FIELDS}

    def add(self, T: int, values: dict[str, dict[str, float]]) -> None:
        """values: symbol -> {field: value}. Replaces the bar if T is already stored."""
        new = [s for s in values if s not in self.symbols]
        if new:
            self.symbols += new
            for f in FIELDS:
                m = self.cols[f]
                self.cols[f] = np.hstack([m, np.full((m.shape[0], len(new)), np.nan, np.float32)])
        if T in self.ts:
            r = self.ts.index(T)
        else:
            self.ts.append(T)
            r = len(self.ts) - 1
            for f in FIELDS:
                m = self.cols[f]
                self.cols[f] = np.vstack([m, np.full((1, len(self.symbols)), np.nan, np.float32)])
        idx = {s: i for i, s in enumerate(self.symbols)}
        for s, v in values.items():
            for f in FIELDS:
                x = v.get(f)
                if x is not None:
                    self.cols[f][r, idx[s]] = x

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp.npz")
        np.savez_compressed(tmp, ts=np.array(self.ts, np.int64), symbols=np.array(self.symbols),
                            **{f: _round(f, self.cols[f]) for f in FIELDS})
        os.replace(tmp, self.path)


def _sig(a: np.ndarray, digits: int) -> np.ndarray:
    """Round to `digits` significant figures (NaN stays NaN)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        mag = np.where(np.isfinite(a) & (a != 0), np.floor(np.log10(np.abs(a))), 0)
        scale = 10.0 ** (digits - 1 - mag)
        return (np.round(a * scale) / scale).astype(np.float32)


def _round(field: str, a: np.ndarray) -> np.ndarray:
    """Keep only meaningful precision: repeated values compress far better than float noise."""
    if field == "spread":
        return np.round(a, 4).astype(np.float32)          # 0.0001 % of mid
    if field == "funding_fc":
        return np.round(a, 5).astype(np.float32)          # 0.00001 % per 8h
    if field == "oi":
        return _sig(a, 5)                                 # 0.001 % resolution for changes
    return _sig(a, 3)                                     # book dollars: 3 significant figures


class MarketRecorder:
    def __init__(self, eng):
        self.eng = eng
        self.dir = Path(eng.cfg.data_dir) / "research" / "market"
        self.dir.mkdir(parents=True, exist_ok=True)
        r = eng.cfg.research
        self.rps = float(r.get("market_rps", 5))
        self.delay_s = float(r.get("market_delay_s", 60))
        self.store: DayStore | None = None

    def coins(self) -> list[str]:
        meta = self.eng.universe.meta
        return sorted(s for s in self.eng.universe.exchange_symbols
                      if (meta.get(s) or {}).get("contractType") == "PERPETUAL" and s.endswith("USDT"))

    async def capture(self, T: int) -> dict[str, dict[str, float]]:
        eng = self.eng
        norm = eng.cfg.funding.normalise_to_min
        out: dict[str, dict[str, float]] = {}
        for s, p in list(eng.premium.data.items()):
            if p.cycle_min:
                out.setdefault(s, {})["funding_fc"] = p.forecast_funding * norm / p.cycle_min * 100
        coins = self.coins()
        gap = 1.0 / max(self.rps, 0.1)

        async def one(sym: str):
            try:
                oi = await eng.rest.open_interest(sym)
                d = await eng.rest.depth(sym, 15)
                sp, bid, ask = book_top(d.get("bids", []), d.get("asks", []))
                out.setdefault(sym, {}).update(oi=float(oi["openInterest"]), spread=sp, bid05=bid, ask05=ask)
            except Exception as e:  # noqa: BLE001 - one coin failing never stops the capture
                log.debug("market %s: %s", sym, e)
        tasks = []
        for sym in coins:                         # paced: ~rps coins per second, 3 weight each
            tasks.append(asyncio.create_task(one(sym)))
            await asyncio.sleep(gap)
        await asyncio.gather(*tasks)
        return {s: v for s, v in out.items() if s in set(coins)}

    def write(self, T: int, values: dict) -> None:
        day = datetime.fromtimestamp(T / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        path = self.dir / f"market-{day}.npz"
        if self.store is None or self.store.path != path:
            self.store = DayStore(path)
        self.store.add(T, values)
        self.store.save()

    async def loop(self) -> None:
        """After every 5m bar (+ market_delay_s, clear of the scanner's own bar work)."""
        while True:
            now = self.eng.clock.now_ms()
            T = now - now % M5 + M5
            await asyncio.sleep((T - now) / 1000 + self.delay_s)
            try:
                vals = await self.capture(T)
                await asyncio.to_thread(self.write, T, vals)
                log.info("market: %d coins recorded for %s", len(vals), T)
            except Exception:  # noqa: BLE001
                log.exception("market recorder failed")
