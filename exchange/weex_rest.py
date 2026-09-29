"""WEEX Futures V3 public REST client (market data only; this app never trades).

Endpoints and weights are from the official docs, verified live in M0
(docs/M0_capability_report.md).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from core.clock import TF_MS
from exchange.models import Bar, normalize_klines
from exchange.ratelimit import WeightLimiter

log = logging.getLogger("weex.rest")

P = "/capi/v3/market"


class WeexError(Exception):
    def __init__(self, status: int, code: Any, msg: str, path: str):
        super().__init__(f"{path}: HTTP {status} code={code} {msg}")
        self.status, self.code, self.msg = status, code, msg


class WeexRest:
    def __init__(self, cfg, clock):
        ex = cfg.exchange
        self.cfg = ex
        self.clock = clock
        self.taker_is_sell = ex.kline_taker_field_is_sell
        self.limiter = WeightLimiter(ex.rate_limit_weight, ex.rate_limit_window_s)
        self.client = httpx.AsyncClient(base_url=ex.rest_base, timeout=ex.http_timeout_s,
                                        headers={"User-Agent": "ignition-coil-scanner/1.0"})
        self.on_incident = None  # callable(kind, message, symbol=None)

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, params: dict | None, weight: int) -> Any:
        attempt = 0
        while True:
            await self.limiter.acquire(weight)
            try:
                r = await self.client.get(P + path, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                err: Exception = e
                status = None
            else:
                status = r.status_code
                used = r.headers.get("x-used-weight-10s")
                if used and used.isdigit():
                    # WEEX counts per IP: other processes (a live scanner + a backtest) share it.
                    self.limiter.sync(int(used))
                    if int(used) > 480:
                        log.warning("server-reported weight %s near limit", used)
                if status == 200:
                    data = r.json()
                    if isinstance(data, dict) and "code" in data and "msg" in data and len(data) <= 3:
                        raise WeexError(status, data["code"], data["msg"], path)
                    return data
                if status in (418, 429):
                    # wait out the whole window: a short Retry-After with many concurrent requests
                    # would just hit the limit again (and repeated 429s can become a 418 IP ban)
                    retry_after = max(float(r.headers.get("Retry-After", "10")), self.cfg.rate_limit_window_s)
                    log.warning("rate limited (%s) on %s, sleeping %.0fs", status, path, retry_after)
                    if self.on_incident:
                        self.on_incident("rate_limit", f"HTTP {status} on {path}")
                    await self.limiter.penalise(retry_after)
                    continue
                if 400 <= status < 500:
                    try:
                        j = r.json()
                    except ValueError:
                        j = {}
                    raise WeexError(status, j.get("code"), j.get("msg", r.text[:200]), path)
                err = WeexError(status, None, r.text[:200], path)
            attempt += 1
            if attempt > self.cfg.max_retries:
                raise err
            delay = min(self.cfg.backoff_base_s * 2 ** (attempt - 1), self.cfg.backoff_max_s)
            delay *= 0.8 + 0.4 * random.random()
            log.warning("GET %s failed (%s), retry %d in %.1fs", path, err, attempt, delay)
            await asyncio.sleep(delay)

    # ---- endpoints -------------------------------------------------------

    async def server_time(self) -> tuple[int, int, int]:
        before = int(time.time() * 1000)
        d = await self._get("/time", None, 1)
        after = int(time.time() * 1000)
        return int(d["serverTime"]), before, after

    async def exchange_info(self, contract_type: str | None = None) -> dict:
        params = {"contractType": contract_type} if contract_type else None
        return await self._get("/exchangeInfo", params, 1)

    async def ticker_24h_all(self) -> list[dict]:
        return await self._get("/ticker/24hr", None, 40)

    async def premium_index_all(self) -> list[dict]:
        return await self._get("/premiumIndex", None, 1)

    async def open_interest(self, symbol: str) -> dict:
        return await self._get("/openInterest", {"symbol": symbol}, 2)

    async def depth(self, symbol: str, limit: int = 200) -> dict:
        return await self._get("/depth", {"symbol": symbol, "limit": limit}, 1)

    async def risk_limits_all(self) -> list[dict]:
        return await self._get("/riskLimits", None, 1)

    async def recent_trades(self, symbol: str, limit: int = 1000) -> list[dict]:
        return await self._get("/trades", {"symbol": symbol, "limit": limit}, 5)

    async def klines(self, symbol: str, tf: str, limit: int) -> list[Bar]:
        """Most recent closed bars (up to 1000), oldest-first."""
        rows = await self._get("/klines", {"symbol": symbol, "interval": tf, "limit": min(limit, 1000)}, 1)
        return normalize_klines(rows, self.clock.now_ms(), self.taker_is_sell)

    async def mark_klines(self, symbol: str, tf: str, limit: int) -> list[Bar]:
        """Most recent closed MARK-price bars (up to 1000), oldest-first."""
        rows = await self._get("/markPriceKlines", {"symbol": symbol, "interval": tf, "limit": min(limit, 1000)}, 1)
        return normalize_klines(rows, self.clock.now_ms(), self.taker_is_sell)

    async def history_klines(self, symbol: str, tf: str, start_ms: int, end_ms: int,
                             price_type: str = "LAST") -> list[Bar]:
        """Closed bars in [start_ms, end_ms], paging backwards (100 bars/request, <=90d window)."""
        step = TF_MS[tf]
        out: dict[int, Bar] = {}
        end = end_ms
        while end >= start_ms:
            start = max(start_ms, end - 99 * step)
            rows = await self._get("/historyKlines", {
                "symbol": symbol, "interval": tf, "startTime": start, "endTime": end,
                "limit": 100, "priceType": price_type}, 5)
            bars = normalize_klines(rows, self.clock.now_ms(), self.taker_is_sell)
            for b in bars:
                if start_ms <= b.t <= end_ms:
                    out[b.t] = b
            if not rows:
                break
            end = start - step
        return [out[t] for t in sorted(out)]

    async def funding_history(self, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
        out: list[dict] = []
        week = 7 * 86_400_000
        s = start_ms
        while s < end_ms:
            e = min(s + week - 1, end_ms)
            out += await self._get("/fundingRate", {"symbol": symbol, "startTime": s, "endTime": e, "limit": 1000}, 5)
            s = e + 1
        return sorted({int(x["fundingTime"]): x for x in out}.values(), key=lambda x: int(x["fundingTime"]))
