"""Sliding-window request-weight limiter (WEEX: weight per rolling window per IP)."""
from __future__ import annotations

import asyncio
import time
from collections import deque


class WeightLimiter:
    def __init__(self, max_weight: int, window_s: float, clock=time.monotonic):
        self.max_weight = max_weight
        self.window_s = window_s
        self._clock = clock
        self._events: deque[tuple[float, int]] = deque()
        self._used = 0
        self._lock = asyncio.Lock()

    def _expire(self, now: float) -> None:
        while self._events and now - self._events[0][0] >= self.window_s:
            _, w = self._events.popleft()
            self._used -= w

    def used(self) -> int:
        self._expire(self._clock())
        return self._used

    async def acquire(self, weight: int) -> None:
        weight = min(weight, self.max_weight)
        async with self._lock:
            while True:
                now = self._clock()
                self._expire(now)
                if self._used + weight <= self.max_weight:
                    self._events.append((now, weight))
                    self._used += weight
                    return
                wait = self.window_s - (now - self._events[0][0]) + 0.01
                await asyncio.sleep(max(wait, 0.01))

    def sync(self, server_used: int) -> None:
        """Fold in weight the server has counted for our IP that we didn't send ourselves
        (other processes on the same IP). Keeps the COMBINED usage under max_weight."""
        now = self._clock()
        self._expire(now)
        extra = server_used - self._used
        if extra > 0:
            self._events.append((now, extra))
            self._used += extra

    async def penalise(self, seconds: float) -> None:
        """Server said slow down: block the whole window for `seconds`."""
        async with self._lock:
            await asyncio.sleep(seconds)
