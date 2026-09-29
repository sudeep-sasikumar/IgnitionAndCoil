"""WEEX Futures V3 public WebSocket: trade streams across several connections.

Limits (WEEX docs): <=100 channels per connection, 240 subscribe ops/hour/connection,
<=20 connections per IP. Server sends {"event":"ping"}; we reply {"method":"PONG"}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Callable

import websockets

from exchange.models import Trade, parse_ws_trade

log = logging.getLogger("weex.ws")

SILENCE_RECONNECT_S = 30


class _Conn:
    def __init__(self, idx: int, url: str, channels: list[str], batch: int,
                 on_trade: Callable[[str, Trade], None], owner: "PublicStream"):
        self.idx, self.url, self.channels, self.batch = idx, url, channels, batch
        self.on_trade = on_trade
        self.owner = owner
        self.task: asyncio.Task | None = None
        self.last_msg = 0.0
        self.connected = False

    async def run(self) -> None:
        attempt = 0
        cfg = self.owner.cfg
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=None, open_timeout=15,
                                              max_size=2 ** 22) as ws:
                    for i in range(0, len(self.channels), self.batch):
                        await ws.send(json.dumps({"method": "SUBSCRIBE",
                                                  "params": self.channels[i:i + self.batch], "id": i + 1}))
                    self.connected = True
                    if attempt:
                        self.owner.incident("ws_reconnect", f"conn {self.idx} reconnected after {attempt} attempt(s)")
                    attempt = 0
                    self.last_msg = time.monotonic()
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=SILENCE_RECONNECT_S)
                        self.last_msg = time.monotonic()
                        self.owner.last_msg = self.last_msg
                        reply = self._handle(raw)
                        if reply:
                            await ws.send(reply)
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as e:  # noqa: BLE001 - any failure -> reconnect with backoff
                self.connected = False
                attempt += 1
                delay = min(cfg.backoff_base_s * 2 ** (attempt - 1), cfg.backoff_max_s) * (0.8 + 0.4 * random.random())
                log.warning("ws conn %d dropped (%s: %s); reconnect in %.1fs", self.idx, type(e).__name__, e, delay)
                if attempt == 1:
                    self.owner.incident("ws_disconnect", f"conn {self.idx}: {type(e).__name__} {e}")
                await asyncio.sleep(delay)

    def _handle(self, raw) -> str | None:
        """Process one message; returns a reply to send (PONG) or None."""
        try:
            m = json.loads(raw)
        except ValueError:
            return None
        if m.get("event") == "ping":
            return json.dumps({"method": "PONG", "id": 1})
        if m.get("e") == "trade":
            sym = m.get("s")
            for d in m.get("d", []):
                try:
                    self.on_trade(sym, parse_ws_trade(d))
                except (KeyError, ValueError, TypeError):
                    log.debug("bad trade payload %s", d)
        # tradeSnapshot (recent trades sent on subscribe) is ignored: those trades may belong to
        # a bucket we started observing mid-way; the trade check skips incomplete buckets anyway.
        return None


class PublicStream:
    def __init__(self, cfg, on_trade: Callable[[str, Trade], None], incident_cb=None):
        self.cfg = cfg.exchange
        self.on_trade = on_trade
        self.incident_cb = incident_cb
        self.conns: list[_Conn] = []
        self.symbols: tuple[str, ...] = ()
        self.last_msg = 0.0

    def incident(self, kind: str, msg: str) -> None:
        if self.incident_cb:
            self.incident_cb(kind, msg)

    @property
    def connected(self) -> bool:
        return bool(self.conns) and all(c.connected for c in self.conns)

    async def set_symbols(self, symbols: list[str]) -> None:
        syms = tuple(sorted(set(symbols)))
        if syms == self.symbols:
            return
        await self.stop()
        self.symbols = syms
        channels = [f"{s}@trade" for s in syms]
        per = self.cfg.ws_channels_per_conn
        for i in range(0, len(channels), per):
            c = _Conn(len(self.conns), self.cfg.ws_public, channels[i:i + per], self.cfg.ws_sub_batch,
                      self.on_trade, self)
            c.task = asyncio.create_task(c.run(), name=f"ws-{c.idx}")
            self.conns.append(c)
        log.info("ws: %d symbols over %d connection(s)", len(syms), len(self.conns))

    async def stop(self) -> None:
        for c in self.conns:
            if c.task:
                c.task.cancel()
        for c in self.conns:
            if c.task:
                try:
                    await c.task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self.conns = []
