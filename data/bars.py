"""In-memory ring buffers of CLOSED bars, per symbol and timeframe."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from exchange.models import Bar


@dataclass(frozen=True)
class BarArrays:
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    qv: np.ndarray
    tbv: np.ndarray
    tbqv: np.ndarray
    tc: np.ndarray

    def __len__(self) -> int:
        return len(self.t)

    def upto(self, as_of_ms: int) -> "BarArrays":
        """Bars that had closed by as_of_ms (close time <= as_of). The no-lookahead cut."""
        n = int(np.searchsorted(self.tc, as_of_ms, side="right"))
        return self.head(n)

    def upto_tail(self, as_of_ms: int, n: int) -> "BarArrays":
        """upto(as_of_ms).tail(n) in a single slice."""
        hi = int(np.searchsorted(self.tc, as_of_ms, side="right"))
        lo = max(0, hi - n)
        return BarArrays(*(getattr(self, f)[lo:hi] for f in _FIELDS))

    def head(self, n: int) -> "BarArrays":
        return BarArrays(*(getattr(self, f)[:n] for f in _FIELDS))

    def tail(self, n: int) -> "BarArrays":
        return BarArrays(*(getattr(self, f)[-n:] if n else getattr(self, f)[:0] for f in _FIELDS))

    @staticmethod
    def from_bars(bars: list[Bar]) -> "BarArrays":
        if not bars:
            e = np.empty(0)
            return BarArrays(*(e for _ in _FIELDS))
        a = np.array(bars, dtype=float)
        # Bar fields: t o h l c v qv tbv tbqv n tc
        return BarArrays(t=a[:, 0].astype(np.int64), o=a[:, 1], h=a[:, 2], l=a[:, 3], c=a[:, 4],
                         v=a[:, 5], qv=a[:, 6], tbv=a[:, 7], tbqv=a[:, 8], tc=a[:, 10].astype(np.int64))


_FIELDS = ("t", "o", "h", "l", "c", "v", "qv", "tbv", "tbqv", "tc")


class BarSeries:
    """Closed bars, oldest-first, capped at maxlen. Merging replaces bars with the same open time."""

    def __init__(self, maxlen: int):
        self.maxlen = maxlen
        self._bars: dict[int, Bar] = {}
        self._cache: BarArrays | None = None

    def merge(self, bars: list[Bar]) -> int:
        """Insert/replace bars; returns the number of new open times added."""
        added = 0
        for b in bars:
            if b.t not in self._bars:
                added += 1
            self._bars[b.t] = b
        if len(self._bars) > self.maxlen:
            for t in sorted(self._bars)[: len(self._bars) - self.maxlen]:
                del self._bars[t]
        self._cache = None
        return added

    def __len__(self) -> int:
        return len(self._bars)

    @property
    def last(self) -> Bar | None:
        return self._bars[max(self._bars)] if self._bars else None

    def arrays(self) -> BarArrays:
        if self._cache is None:
            self._cache = BarArrays.from_bars([self._bars[t] for t in sorted(self._bars)])
        return self._cache


class CandleStore:
    def __init__(self, keep: dict[str, int]):
        self.keep = keep
        self._s: dict[str, dict[str, BarSeries]] = {}

    def series(self, symbol: str, tf: str) -> BarSeries:
        by_tf = self._s.setdefault(symbol, {})
        if tf not in by_tf:
            by_tf[tf] = BarSeries(self.keep[tf])
        return by_tf[tf]

    def has(self, symbol: str) -> bool:
        return symbol in self._s

    def drop(self, symbol: str) -> None:
        self._s.pop(symbol, None)

    def symbols(self) -> list[str]:
        return list(self._s)
