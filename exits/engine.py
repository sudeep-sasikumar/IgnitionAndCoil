"""Hybrid exit engine (spec §7) - a pure, serialisable state machine.

Phase 1 (entry -> TP1): stop fixed at the initial stop. Time stop: exit if +1% is not reached
    within 45 min.
TP1 (+3%): close 40%; stop -> breakeven + round-trip fees.
Phase 2 (TP1 -> TP2): stop = max(BE + fees, chandelier); chandelier = highest high since entry
    - 2.5 x ATR15m, recomputed on each closed 15m bar.
TP2 = min(+6%, resistance - 0.2%), fixed at entry: close 30% (dropped -> runner gets it).
Phase 3 (runner): stop = max(chandelier, last confirmed 15m swing low - 0.1 x ATR15m); exit on a
    15m close below EMA20(15m).
The stop only ever moves up. Max hold 8h.

Inputs are price samples (live: tape high/low + mark every second; replay/backtest: 5m bars)
and closed 15m bars. The same code runs everywhere (paper trades, your logged trades' action
alerts, late-logging replay, restart catch-up, backtests).
Within one sample the stop is checked BEFORE targets (conservative for a long).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

PHASE_1, PHASE_2, PHASE_RUNNER = 1, 2, 3

# exit reasons / event kinds
TP1, TP2, STOP, TIME_STOP, EMA_EXIT, MAX_HOLD, LIQUIDATION, STOP_MOVE = (
    "TP1", "TP2", "STOP", "TIME_STOP", "EMA_EXIT", "MAX_HOLD", "LIQUIDATION", "STOP_MOVE")
MARKET_EXITS = {TIME_STOP, EMA_EXIT, MAX_HOLD}


@dataclass
class ExitParams:
    entry: float                 # entry price (fill) the plan is measured from
    entry_ms: int
    initial_stop: float
    tp1: float
    tp1_frac: float
    tp2: float | None
    tp2_frac: float
    be_stop: float               # breakeven + round-trip fees
    time_stop_ms: int            # absolute deadline for reaching +1%
    time_stop_price: float
    max_hold_until_ms: int
    chandelier_mult: float
    swing_atr_mult: float
    liq_price: float | None = None


@dataclass
class ExitEvent:
    ts: int
    kind: str
    price: float
    fraction: float              # fraction of the ORIGINAL position closed (0 for stop moves)
    stop_before: float
    stop_after: float
    note: str = ""


@dataclass
class ExitState:
    p: ExitParams
    phase: int = PHASE_1
    stop: float = 0.0
    remaining: float = 1.0
    highest: float = 0.0
    lowest: float = 0.0
    reached_1pct: bool = False
    chandelier: float | None = None
    tp1_ms: int | None = None
    tp2_ms: int | None = None
    closed: bool = False
    exit_reason: str | None = None
    exit_ms: int | None = None
    last_ts: int = 0
    last_15m_ms: int = 0
    events: list[ExitEvent] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ExitState":
        d = dict(d)
        p = ExitParams(**d.pop("p"))
        evs = [ExitEvent(**e) for e in d.pop("events", [])]
        return ExitState(p=p, events=evs, **d)


def make_params(entry: float, entry_ms: int, stop: float, tp1: float, tp2: float | None, cfg,
                liq_price: float | None = None) -> ExitParams:
    p, t = cfg.plan, cfg.trade
    tp1_frac = p.tp1_close_pct / 100
    tp2_frac = p.tp2_close_pct / 100 if tp2 else 0.0
    be = entry * (1 + 2 * t.taker_fee + t.slippage_pct / 100)
    return ExitParams(
        entry=entry, entry_ms=entry_ms, initial_stop=stop, tp1=tp1, tp1_frac=tp1_frac, tp2=tp2,
        tp2_frac=tp2_frac, be_stop=be, time_stop_ms=entry_ms + p.time_stop_min * 60_000,
        time_stop_price=entry * (1 + p.time_stop_target_pct / 100),
        max_hold_until_ms=entry_ms + int(p.max_hold_h * 3_600_000), chandelier_mult=p.chandelier_atr15,
        swing_atr_mult=p.runner_swing_atr15, liq_price=liq_price)


def new_state(params: ExitParams) -> ExitState:
    return ExitState(p=params, stop=params.initial_stop, highest=params.entry, lowest=params.entry,
                     last_ts=params.entry_ms, last_15m_ms=params.entry_ms)


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def _close(st: ExitState, ts: int, kind: str, price: float, note: str = "") -> ExitEvent:
    ev = ExitEvent(ts, kind, price, st.remaining, st.stop, st.stop, note)
    st.remaining = 0.0
    st.closed, st.exit_reason, st.exit_ms = True, kind, ts
    st.events.append(ev)
    return ev


def _raise_stop(st: ExitState, ts: int, new_stop: float, note: str) -> ExitEvent | None:
    if new_stop <= st.stop + 1e-12:
        return None               # the stop only ever moves up
    ev = ExitEvent(ts, STOP_MOVE, new_stop, 0.0, st.stop, new_stop, note)
    st.stop = new_stop
    st.events.append(ev)
    return ev


def on_prices(st: ExitState, ts: int, trigger_low: float, high: float, last: float,
              mark_low: float | None = None, open_price: float | None = None) -> list[ExitEvent]:
    """One price sample: [trigger_low, high] seen since the previous sample, `last` = latest price.

    trigger_low: low of the stop-trigger price (mark or last, per config).
    mark_low: low of mark price for the liquidation check (always mark).
    open_price: bar open (replay) - a stop gapped through fills at the open, not the stop.
    """
    if st.closed or ts <= st.p.entry_ms:
        return []
    out: list[ExitEvent] = []
    st.last_ts = max(st.last_ts, ts)
    st.lowest = min(st.lowest, trigger_low)

    liq = st.p.liq_price
    if _ok(liq) and _ok(mark_low) and mark_low <= liq and st.stop <= liq:
        out.append(_close(st, ts, LIQUIDATION, liq, "mark price reached the liquidation price"))
        return out
    if trigger_low <= st.stop:
        fill = min(st.stop, open_price) if _ok(open_price) else st.stop
        note = "trailing stop" if st.phase > PHASE_1 else "initial stop"
        out.append(_close(st, ts, STOP, fill, note))
        return out

    # targets (after the stop check: conservative)
    st.highest = max(st.highest, high)
    if not st.reached_1pct and high >= st.p.time_stop_price:
        st.reached_1pct = True
    if st.phase == PHASE_1 and high >= st.p.tp1:
        out.append(ExitEvent(ts, TP1, st.p.tp1, st.p.tp1_frac, st.stop, st.stop))
        st.events.append(out[-1])
        st.remaining -= st.p.tp1_frac
        st.tp1_ms = ts
        st.phase = PHASE_2 if st.p.tp2 else PHASE_RUNNER
        mv = _raise_stop(st, ts, st.p.be_stop, "breakeven + fees after TP1")
        if mv:
            out.append(mv)
    if st.phase == PHASE_2 and st.p.tp2 and high >= st.p.tp2:
        out.append(ExitEvent(ts, TP2, st.p.tp2, st.p.tp2_frac, st.stop, st.stop))
        st.events.append(out[-1])
        st.remaining -= st.p.tp2_frac
        st.tp2_ms = ts
        st.phase = PHASE_RUNNER
    if st.remaining <= 1e-9:
        st.closed, st.exit_reason, st.exit_ms, st.remaining = True, TP2, ts, 0.0
        return out

    if st.phase == PHASE_1 and not st.reached_1pct and ts >= st.p.time_stop_ms:
        out.append(_close(st, ts, TIME_STOP, last, f"+{(st.p.time_stop_price / st.p.entry - 1) * 100:.1f}% "
                                                   f"not reached in time"))
        return out
    if ts >= st.p.max_hold_until_ms:
        out.append(_close(st, ts, MAX_HOLD, last, "max hold reached"))
    return out


def on_bar_15m(st: ExitState, ts_close: int, close: float, ema20: float | None, atr: float | None,
               swing_low: float | None) -> list[ExitEvent]:
    """A 15m bar closed at ts_close (only bars that closed after entry count)."""
    if st.closed or ts_close <= st.last_15m_ms or ts_close <= st.p.entry_ms:
        return []
    st.last_15m_ms = ts_close
    out: list[ExitEvent] = []
    if st.phase >= PHASE_2 and _ok(atr):
        st.chandelier = st.highest - st.p.chandelier_mult * atr
        if st.phase == PHASE_2:
            target, note = max(st.p.be_stop, st.chandelier), "chandelier trail"
        else:
            swing = swing_low - st.p.swing_atr_mult * atr if _ok(swing_low) else -math.inf
            target = max(st.chandelier, swing)
            note = "runner trail (chandelier / 15m swing low)"
        if target >= close:
            # trail would sit above the market: the runner is effectively stopped at the close
            st.stop = max(st.stop, min(target, close))
            out.append(_close(st, ts_close, STOP, close, "trail above price at 15m close"))
            return out
        mv = _raise_stop(st, ts_close, target, note)
        if mv:
            out.append(mv)
    if st.phase == PHASE_RUNNER and _ok(ema20) and close < ema20:
        out.append(_close(st, ts_close, EMA_EXIT, close, "15m close below EMA20"))
    return out


def plan_legs(st: ExitState) -> list[tuple[float, float, str]]:
    """(price, fraction, kind) fills produced so far; kind in tp|stop|market for P&L."""
    legs = []
    for e in st.events:
        if e.kind in (TP1, TP2):
            legs.append((e.price, e.fraction, "tp"))
        elif e.kind in (STOP, LIQUIDATION):
            legs.append((e.price, e.fraction, "stop"))
        elif e.kind in MARKET_EXITS:
            legs.append((e.price, e.fraction, "market"))
    return legs
