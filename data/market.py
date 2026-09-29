"""Mark price, index price and funding for all symbols from one premiumIndex call."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Premium:
    symbol: str
    mark: float
    index: float
    last_funding: float        # rate settled at the previous settlement
    forecast_funding: float    # predicted rate for the next settlement
    next_funding_ms: int
    cycle_min: int             # funding interval in minutes (60 / 240 / 480 on WEEX)
    ts: int

    def funding_8h(self, field: str, normalise_to_min: int) -> float:
        """Rate normalised to an 8h-equivalent (as a fraction, e.g. 0.0001 = 0.01%)."""
        rate = self.forecast_funding if field == "forecast" else self.last_funding
        return rate * normalise_to_min / self.cycle_min if self.cycle_min else rate


class PremiumTracker:
    def __init__(self, cfg, rest):
        self.rest = rest
        self.field = cfg.funding.rate_field
        self.norm = cfg.funding.normalise_to_min
        self.data: dict[str, Premium] = {}
        self.last_poll_ms = 0

    async def poll(self) -> None:
        rows = await self.rest.premium_index_all()
        for r in rows:
            try:
                p = Premium(symbol=r["symbol"], mark=float(r["markPrice"]), index=float(r["indexPrice"]),
                            last_funding=float(r["lastFundingRate"]),
                            forecast_funding=float(r["forecastFundingRate"]),
                            next_funding_ms=int(r["nextFundingTime"]), cycle_min=int(r["collectCycle"]),
                            ts=int(r["time"]))
            except (KeyError, ValueError, TypeError):
                continue
            self.data[p.symbol] = p
            self.last_poll_ms = max(self.last_poll_ms, p.ts)

    def funding_8h(self, symbol: str) -> float | None:
        p = self.data.get(symbol)
        return p.funding_8h(self.field, self.norm) if p else None

    def mark(self, symbol: str) -> float | None:
        p = self.data.get(symbol)
        return p.mark if p else None
