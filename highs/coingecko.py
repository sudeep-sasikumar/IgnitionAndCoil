"""CoinGecko public API client (free; no key needed, an optional free Demo key raises limits).

Endpoints used (checked against docs.coingecko.com and live on 2026-09-30):
- GET /coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=N
  -> id, symbol, name, current_price, market_cap, market_cap_rank, total_volume, high_24h,
     ath, ath_date, last_updated (cached by CoinGecko for 60 s on the free tiers)
- GET /coins/{id}/ohlc?vs_currency=usd&days=365 -> [[ts, open, high, low, close], ...] (4-day candles;
  days=1 -> 30-minute candles, days=7/14/30 -> 4-hour candles; ts = the candle's close time;
  checked live on 2026-10-05)
Free tiers only serve the last 365 days of history (error 10012 beyond that).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import httpx

log = logging.getLogger("coingecko")


def parse_iso_ms(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


class CoinGecko:
    def __init__(self, base: str, calls_per_min: float, demo_key: str = ""):
        headers = {"accept": "application/json"}
        if demo_key:
            headers["x-cg-demo-api-key"] = demo_key
        self.client = httpx.AsyncClient(base_url=base.rstrip("/"), headers=headers, timeout=30)
        self.interval = 60.0 / max(calls_per_min, 0.1)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, params: dict):
        """Paced GET (one call at a time, calls_per_min); waits out 429s (Retry-After)."""
        async with self._lock:
            for attempt in range(6):
                wait = self.interval - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
                try:
                    r = await self.client.get(path, params=params)
                except httpx.HTTPError as e:
                    log.warning("GET %s failed (%s), retry %d", path, e, attempt + 1)
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                if r.status_code == 429:
                    retry = float(r.headers.get("retry-after") or 60)
                    log.info("CoinGecko rate limit, waiting %.0fs", retry)
                    await asyncio.sleep(retry)
                    continue
                if r.status_code >= 500:
                    await asyncio.sleep(10 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            raise RuntimeError(f"CoinGecko {path}: giving up after retries")

    async def markets(self, page: int, per_page: int = 250) -> list[dict]:
        return await self._get("/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc",
                                                  "per_page": per_page, "page": page})

    async def top(self, n: int) -> list[dict]:
        out: list[dict] = []
        page = 1
        while len(out) < n:
            rows = await self.markets(page)
            if not rows:
                break
            out += rows
            page += 1
        return out[:n]

    async def ohlc(self, cg_id: str, days: int) -> list[list[float]]:
        """days: 1, 7, 14, 30, 90, 180 or 365 (the only ranges the free tiers serve)."""
        return await self._get(f"/coins/{cg_id}/ohlc", {"vs_currency": "usd", "days": days})

    async def ohlc_year(self, cg_id: str) -> list[list[float]]:
        return await self.ohlc(cg_id, 365)
