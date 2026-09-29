"""Universe filter (spec §3), refreshed every 30 min.

Cheap checks first (volume from one call), then per-symbol: order book, listing age,
ATR(1h)%, and the wick-risk filter.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import numpy as np

from data.bars import BarArrays
from features import indicators as ind

log = logging.getLogger("universe")

DAY_MS = 86_400_000


@dataclass
class Candidate:
    symbol: str
    base: str
    quote_volume_24h: float
    contract_val: float = 0.0
    spread_pct: float = float("nan")
    bid_depth_usd: float = float("nan")
    listing_age_days: float = float("nan")
    atr_1h_pct: float = float("nan")
    deep_wicks: int = -1
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reasons


# ---- pure filter helpers (unit-tested) ----------------------------------

def spread_and_bid_depth(bids: list, asks: list, band_pct: float) -> tuple[float, float]:
    """Spread % of mid, and USD bid depth within band_pct of mid."""
    if not bids or not asks:
        return float("nan"), 0.0
    best_bid = max(float(p) for p, _ in bids)
    best_ask = min(float(p) for p, _ in asks)
    mid = (best_bid + best_ask) / 2
    spread = (best_ask - best_bid) / mid * 100
    floor = mid * (1 - band_pct / 100)
    depth = sum(float(p) * float(q) for p, q in bids if float(p) >= floor)
    return spread, depth


def count_deep_wicks(b5: BarArrays, lookback: int, wick_pct: float) -> int:
    w = ind.lower_wick_pct(b5.o[-lookback:], b5.l[-lookback:], b5.c[-lookback:])
    return int(np.count_nonzero(w >= wick_pct))


def base_filters(info_symbols: list[dict], tickers: dict[str, float], cfg) -> tuple[list[Candidate], dict[str, str]]:
    """Contract type / quote / stablecoin / 24h-volume filters. Returns candidates + rejects."""
    u = cfg.universe
    stables = {s.upper() for s in u.stablecoin_bases}
    out, rejected = [], {}
    for s in info_symbols:
        sym = s["symbol"]
        if s.get("contractType") not in u.contract_types:
            continue
        if s.get("quoteAsset") != u.quote_asset:
            continue
        if s.get("baseAsset", "").upper() in stables:
            rejected[sym] = "stablecoin"
            continue
        qv = tickers.get(sym, 0.0)
        if qv < u.min_quote_volume_24h:
            rejected[sym] = "volume"
            continue
        out.append(Candidate(symbol=sym, base=s.get("baseAsset", ""), quote_volume_24h=qv,
                             contract_val=float(s.get("contractVal") or 0)))
    out.sort(key=lambda c: c.quote_volume_24h, reverse=True)
    return out, rejected


def book_checks(c: Candidate, cfg) -> None:
    u = cfg.universe
    if not (c.spread_pct <= u.max_spread_pct):
        c.reasons.append(f"spread {c.spread_pct:.3f}%")
    if not (c.bid_depth_usd >= u.min_book_depth_usd_0_5pct):
        c.reasons.append(f"depth ${c.bid_depth_usd:,.0f}")


def apply_checks(c: Candidate, cfg) -> None:
    u = cfg.universe
    book_checks(c, cfg)
    if not (c.listing_age_days >= u.min_listing_age_days):
        c.reasons.append(f"age {c.listing_age_days:.1f}d")
    if not (c.atr_1h_pct >= u.min_atr_1h_pct):
        c.reasons.append(f"atr1h {c.atr_1h_pct:.2f}%")
    if c.deep_wicks > u.max_deep_wick_bars:
        c.reasons.append(f"wicks {c.deep_wicks}")


# ---- builder ------------------------------------------------------------

class UniverseBuilder:
    def __init__(self, cfg, rest, db, clock):
        self.cfg, self.rest, self.db, self.clock = cfg, rest, db, clock
        self.symbols: list[str] = []            # signal universe (majors excluded unless configured)
        self.candidates: list[Candidate] = []   # last evaluation, for display
        self.exchange_symbols: set[str] = set() # everything currently listed (delisting detection)
        self.meta: dict[str, dict] = {}         # exchangeInfo row per symbol (pricePrecision etc.)
        self.last_refresh_ms = 0

    async def _listing_age_days(self, sym: str, now: int) -> float:
        first = self.db.first_bar_ts(sym)
        if first is None:
            bars = await self.rest.klines(sym, "1d", 1000)
            if not bars:
                return 0.0
            first = bars[0].t
            if len(bars) >= 30:  # stable enough to cache; young listings are re-checked
                self.db.set_first_bar_ts(sym, first)
        return (now - first) / DAY_MS

    async def _evaluate(self, c: Candidate, now: int) -> None:
        u = self.cfg.universe
        try:
            book = await self.rest.depth(c.symbol, 200)
            c.spread_pct, c.bid_depth_usd = spread_and_bid_depth(book.get("bids", []), book.get("asks", []),
                                                                 u.depth_band_pct)
            book_checks(c, self.cfg)
            if c.reasons:
                return
            c.listing_age_days = await self._listing_age_days(c.symbol, now)
            h1 = BarArrays.from_bars(await self.rest.klines(c.symbol, "1h", 100))
            if len(h1) > u.atr_period:
                c.atr_1h_pct = float(ind.atr(h1.h, h1.l, h1.c, u.atr_period)[-1] / h1.c[-1] * 100)
            m5 = BarArrays.from_bars(await self.rest.klines(c.symbol, "5m", u.wick_lookback_bars_5m + 2))
            c.deep_wicks = count_deep_wicks(m5, u.wick_lookback_bars_5m, u.deep_wick_pct)
        except Exception as e:  # noqa: BLE001
            c.reasons.append(f"error {type(e).__name__}")
            log.warning("universe check %s failed: %s", c.symbol, e)
            return
        apply_checks(c, self.cfg)

    async def refresh(self) -> list[str]:
        u = self.cfg.universe
        now = self.clock.now_ms()
        info = await self.rest.exchange_info()
        self.exchange_symbols = {s["symbol"] for s in info["symbols"]}
        self.meta = {s["symbol"]: s for s in info["symbols"]}
        tick = await self.rest.ticker_24h_all()
        vols = {t["symbol"]: float(t.get("quoteVolume") or 0) for t in tick}
        cands, _rej = base_filters(info["symbols"], vols, self.cfg)
        majors = set(u.majors)
        await asyncio.gather(*(self._evaluate(c, now) for c in cands if c.symbol not in majors))
        passing = [c for c in cands if c.symbol not in majors and c.ok]
        if u.include_majors:
            passing = [c for c in cands if c.symbol in majors] + passing
            passing.sort(key=lambda c: c.quote_volume_24h, reverse=True)
        self.symbols = [c.symbol for c in passing[: u.max_symbols]]
        self.candidates = cands
        self.last_refresh_ms = now
        log.info("universe: %d candidates by volume, %d pass, %d kept", len(cands), len(passing), len(self.symbols))
        return self.symbols
