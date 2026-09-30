"""Short positions through the UNCHANGED long exit engine - exactly, not approximately.

A short on price P behaves like a long on x = -P. Every rule of the exit engine is linear in
price: stops and targets, breakeven, chandelier (highest high - k x ATR), the 15m swing low,
the EMA20 exit and gap fills at the bar open. ATR is unchanged by negation and EMA(-P) =
-EMA(P), and negating a bar swaps its high and low. So the bars and the plan's prices are
negated, the engine runs as-is, and the resulting event prices are negated back.

Only breakeven (+fees) and the time-stop level are built directly: they are percentages of the
entry in the short's own direction.
"""
from __future__ import annotations

import dataclasses

from data.bars import BarArrays
from exits.context import replay
from exits.engine import TIME_STOP, ExitEvent, ExitParams, ExitState, make_params


def negate_bars(b: BarArrays) -> BarArrays:
    return BarArrays(b.t, -b.o, -b.l, -b.h, -b.c, b.v, b.qv, b.tbv, b.tbqv, b.tc)


def make_params_short(fill: float, entry_ms: int, stop: float, tp1: float, tp2: float | None, cfg,
                      liq_price: float | None = None) -> ExitParams:
    """Exit parameters of a short, in the negated price space the engine runs in."""
    p, t = cfg.plan, cfg.trade
    base = make_params(-fill, entry_ms, -stop, -tp1, -tp2 if tp2 else None, cfg,
                       liq_price=-liq_price if liq_price else None)
    return dataclasses.replace(
        base,
        be_stop=-fill * (1 - 2 * t.taker_fee - t.slippage_pct / 100),       # breakeven + fees, below entry
        time_stop_price=-fill * (1 - p.time_stop_target_pct / 100))         # -1% must be reached in time


def _unflip(e: ExitEvent, cfg) -> ExitEvent:
    note = e.note
    if e.kind == TIME_STOP:
        note = f"-{cfg.plan.time_stop_target_pct:.1f}% not reached in time"
    return ExitEvent(e.ts, e.kind, -e.price, e.fraction, -e.stop_before, -e.stop_after, note)


def replay_short(st: ExitState, b5: BarArrays, b15: BarArrays, cfg, until_ms: int,
                 mark5: BarArrays | None = None) -> list[ExitEvent]:
    """replay() for a short state from make_params_short; returns events in real prices."""
    evs = replay(st, negate_bars(b5), negate_bars(b15), cfg, until_ms,
                 mark5=negate_bars(mark5) if mark5 is not None and len(mark5) else None)
    return [_unflip(e, cfg) for e in evs]


def price_range(st: ExitState) -> tuple[float, float]:
    """(lowest, highest) real price seen since entry by a short state."""
    return -st.highest, -st.lowest
