"""Live trade tape: 5m taker-buy/sell buckets, last price, and a per-bar cross-check
against the kline-derived taker volume (the canonical CVD source; see M0 report)."""
from __future__ import annotations

import logging
from collections import deque

from core.clock import TF_MS
from exchange.models import Bar, Trade

log = logging.getLogger("orderflow")

BUCKET = TF_MS["5m"]


class TradeTape:
    def __init__(self, cfg):
        self.tol = cfg.orderflow.trade_check_tolerance
        size = cfg.orderflow.trade_dedupe_size
        self._seen: set[str] = set()
        self._order: deque[str] = deque(maxlen=size)
        self.buckets: dict[str, dict[int, list[float]]] = {}   # sym -> bucket_start -> [buy_q, sell_q, total_q]
        self.observed_since: dict[str, int] = {}               # first trade time since (re)connect
        self.last_price: dict[str, float] = {}
        self.last_trade_ms: dict[str, int] = {}
        self.checks_ok = 0
        self.checks_bad = 0
        self._window: dict[str, list[float]] = {}               # sym -> [low, high] since last drain

    def drain(self, sym: str) -> tuple[float, float, float] | None:
        """(low, high, last) of trades since the previous drain - so trade tracking sees every
        wick between samples. Falls back to the last price when nothing traded."""
        last = self.last_price.get(sym)
        if last is None:
            return None
        w = self._window.pop(sym, None)
        return (w[0], w[1], last) if w else (last, last, last)

    def reset_observation(self) -> None:
        """Call after a WS disconnect: buckets spanning the gap are incomplete."""
        self.observed_since.clear()

    def on_trade(self, sym: str, tr: Trade) -> None:
        if tr.id in self._seen:
            return
        if len(self._order) == self._order.maxlen:
            self._seen.discard(self._order[0])
        self._order.append(tr.id)
        self._seen.add(tr.id)

        self.observed_since.setdefault(sym, tr.t)
        self.last_price[sym] = tr.price
        w = self._window.get(sym)
        if w is None:
            self._window[sym] = [tr.price, tr.price]
        else:
            w[0], w[1] = min(w[0], tr.price), max(w[1], tr.price)
        self.last_trade_ms[sym] = max(tr.t, self.last_trade_ms.get(sym, 0))
        b = self.buckets.setdefault(sym, {})
        k = tr.t - tr.t % BUCKET
        cell = b.setdefault(k, [0.0, 0.0, 0.0])
        cell[0 if tr.taker_buy else 1] += tr.quote
        cell[2] += tr.quote
        if len(b) > 30:
            for old in sorted(b)[:-30]:
                del b[old]

    def check_bar(self, sym: str, bar: Bar) -> str | None:
        """Compare tape vs kline for a closed 5m bar.

        Returns None if OK or not checkable, else a mismatch description.
        """
        since = self.observed_since.get(sym)
        cell = self.buckets.get(sym, {}).get(bar.t)
        if since is None or since > bar.t or cell is None or bar.qv <= 0:
            return None  # we did not observe the whole bar
        buy, _sell, total = cell
        if abs(total - bar.qv) / bar.qv > self.tol:
            return None  # tape incomplete (missed messages); not a semantic problem
        diff = abs(buy - bar.tbqv) / bar.qv
        if diff <= self.tol:
            self.checks_ok += 1
            return None
        self.checks_bad += 1
        return (f"tape taker-buy {buy:,.0f} vs kline-derived {bar.tbqv:,.0f} "
                f"(bar vol {bar.qv:,.0f}, diff {diff:.1%})")
