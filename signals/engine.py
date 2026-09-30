"""Signal generation shared by the live engine and the backtester.

Per closed 5m bar and symbol: levels/headroom -> Ignition evaluation -> Coil WATCH update
-> (at 15m boundaries) Coil ENTRY evaluation -> score -> trade plan -> Signal.
Tags: session, weekend, RISK_OFF, BLACKOUT, PRICE_DISCOVERY. Per-symbol cooldown applies here;
Telegram-side gating (pause, hourly cap, suppression) is done by the alerting layer.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from data.bars import BarArrays
from features.compute import Features
from levels.resistance import LevelMap, build_levels, headroom, headroom_down
from plan.liquidation import Bracket
from plan.trade_plan import TradePlan, build_plan
from signals.regime import RISK_OFF, Regime
from signals.score import compute_score
from signals.setups import (COIL, COIL_SHORT, IGNITION, IGNITION_SHORT, LONG, SHORT, CoilTracker, CoilWatch,
                            SetupEval, eval_ignition, eval_ignition_short, off, side_of)

ENTRY, WATCH, SKIP = "ENTRY", "WATCH", "SKIP"


@dataclass
class Signal:
    symbol: str
    setup: str
    bar_close_ms: int
    ref_entry: float
    score: int
    breakdown: dict
    conds: list[dict]
    features: dict
    regime: dict
    plan: TradePlan
    headroom: dict
    levels: list[dict]
    session: str
    tags: list[str]
    extra: dict
    signal_id: str | None = None
    suppressed_reason: str | None = None
    side: str = LONG

    def to_record(self) -> dict:
        d = dataclasses.asdict(self)
        return d


@dataclass
class ScanRow:
    symbol: str
    price: float
    state: str
    score: int                  # best score among evaluated setups this bar
    setup: str                  # setup the score refers to
    n_pass: int
    n_conds: int
    headroom_pct: float
    price_discovery: bool
    features: Features
    watch: CoilWatch | None = None
    failed: list[str] = field(default_factory=list)
    ignition_conds: list[dict] = field(default_factory=list)
    watch_conds: list[dict] = field(default_factory=list)
    coil_entry_conds: list[dict] = field(default_factory=list)
    breakdown: dict = field(default_factory=dict)


def session_tags(ms: int, cfg) -> tuple[str, list[str]]:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    name = "UNKNOWN"
    for k, (a, b) in cfg.signals.sessions_utc.to_dict().items():
        if a <= dt.hour < b:
            name = k
            break
    tags = [name]
    if dt.weekday() >= 5:
        tags.append("WEEKEND")
    return name, tags


def parse_blackouts(cfg) -> list[tuple[int, int, str]]:
    tz = ZoneInfo(cfg.app.display_tz)
    out = []
    for w in cfg.signals.blackout_windows or []:
        s = datetime.strptime(w["start"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        e = datetime.strptime(w["end"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        out.append((int(s.timestamp() * 1000), int(e.timestamp() * 1000), w.get("label", "blackout")))
    return out


def _levels_dump(lm: LevelMap, price: float) -> list[dict]:
    near = [lv for lv in lm.levels if price * 0.7 <= lv.price <= price * 1.5]
    return [{"price": lv.price, "kind": lv.kind} for lv in sorted(near, key=lambda x: x.price)]


class SignalEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.coil = CoilTracker(cfg)
        self.coil_enabled = bool(cfg.coil.get("enabled", True))
        short = cfg.get("short")
        self.short_enabled = bool(short and short.get("enabled"))
        self.coil_short = CoilTracker(cfg, SHORT)
        self.disabled = set(cfg.signals.disabled_conditions or [])
        self.last_signal_ms: dict[str, int] = {}
        self.blackouts = parse_blackouts(cfg)
        self._levels: dict[str, tuple[tuple, LevelMap]] = {}

    def levels(self, sym: str, b1h: BarArrays, b4h: BarArrays) -> LevelMap:
        key = (int(b1h.t[-1]) if len(b1h) else 0, int(b4h.t[-1]) if len(b4h) else 0, len(b1h), len(b4h))
        hit = self._levels.get(sym)
        if hit and hit[0] == key:
            return hit[1]
        lm = build_levels(b1h, b4h, self.cfg)
        self._levels[sym] = (key, lm)
        return lm

    def blackout_label(self, ms: int) -> str | None:
        for s, e, label in self.blackouts:
            if s <= ms < e:
                return label
        return None

    def _make_signal(self, ev: SetupEval, sym: str, as_of: int, f: Features, score: int, breakdown: dict,
                     regime: Regime | None, lm: LevelMap, ref: float, brackets: list[Bracket] | None,
                     precision: int | None) -> Signal:
        cfg = self.cfg
        side = side_of(ev.setup)
        hr_ref = (headroom_down if side == SHORT else headroom)(lm, ref, f.atr_1h_pct, cfg)
        res = hr_ref.level.price if hr_ref.level else None
        plan = build_plan(sym, ev.setup, ref, ev.structural_stop, res, brackets, cfg, precision=precision, side=side)
        session, tags = session_tags(as_of, cfg)
        if side == SHORT:
            tags.append("SHORT")
            if regime is not None:
                tags.append(f"REGIME:{regime.state}")     # shorts are gated by short.regime_rule
        elif regime is not None and regime.state == RISK_OFF:
            tags.append("RISK_OFF")
        bl = self.blackout_label(as_of)
        if bl:
            tags.append(f"BLACKOUT:{bl}")
        if hr_ref.price_discovery:
            tags.append("PRICE_DISCOVERY")
        return Signal(
            symbol=sym, setup=ev.setup, bar_close_ms=as_of, ref_entry=ref, score=score, breakdown=breakdown,
            conds=[dataclasses.asdict(c) for c in ev.conds], features=f.as_dict(),
            regime=regime.as_dict() if regime else {}, plan=plan,
            headroom={"pct": hr_ref.pct, "level": res, "kind": hr_ref.level.kind if hr_ref.level else None,
                      "price_discovery": hr_ref.price_discovery},
            levels=_levels_dump(lm, ref), session=session, tags=tags, extra=ev.extra, side=side)

    def evaluate(self, sym: str, as_of: int, f: Features, b5: BarArrays, b15: BarArrays, b1h: BarArrays,
                 b4h: BarArrays, regime: Regime | None, brackets: list[Bracket] | None, ref_price: float,
                 precision: int | None = None, dry_run: bool = False) -> tuple[ScanRow, list[Signal], CoilWatch | None]:
        """Evaluate one symbol on the 5m bar that closed at as_of (all inputs cut at as_of).
        dry_run: evaluate for display only - no cooldown recorded, no WATCH consumed."""
        cfg = self.cfg
        lm = self.levels(sym, b1h, b4h)
        hr = headroom(lm, f.price, f.atr_1h_pct, cfg)
        rstate = regime.state if regime else "NEUTRAL"

        evals: list[SetupEval] = [eval_ignition(f, b5, hr, cfg, self.disabled)]
        watch_conds, new_watch = self.coil.update(sym, as_of, f, b1h, self.disabled) if self.coil_enabled else ([], False)
        coil_entry_conds: list[dict] = []
        if self.coil_enabled and as_of % 900_000 == 0:  # a 15m bar closed together with this 5m bar
            ce = self.coil.eval_entry(sym, f, b15, hr, self.disabled)
            if ce:
                evals.append(ce)
                coil_entry_conds = [vars(c).copy() for c in ce.conds]

        best = None
        signals: list[Signal] = []
        cooling = as_of - self.last_signal_ms.get(sym, -10 ** 15) < cfg.signals.symbol_cooldown_min * 60_000
        for ev in evals:
            score, breakdown = compute_score(ev.setup, f, rstate, hr, cfg, self.disabled)
            if best is None or (ev.hard_pass, score) > (best[0].hard_pass, best[1]):
                best = (ev, score, breakdown)
            if ev.hard_pass and score >= cfg.score.min_entry and not cooling and not signals:
                signals.append(self._make_signal(ev, sym, as_of, f, score, breakdown, regime, lm,
                                                 ref_price, brackets, precision))
                if not dry_run:
                    self.last_signal_ms[sym] = as_of
                    if ev.setup == COIL:
                        self.coil.consume(sym)

        if self.short_enabled:
            signals += self._evaluate_short(sym, as_of, f, b5, b15, b1h, lm, regime, rstate, brackets,
                                            ref_price, precision, dry_run)

        ev, score, breakdown = best
        state = ENTRY if signals else WATCH if sym in self.coil.watches else SKIP
        row = ScanRow(symbol=sym, price=f.price, state=state, score=score, setup=ev.setup, n_pass=ev.n_pass,
                      n_conds=len(ev.conds), headroom_pct=hr.pct, price_discovery=hr.price_discovery,
                      features=f, watch=self.coil.watches.get(sym),
                      failed=[c.name for c in ev.conds if not c.passed and not off(c, self.disabled)],
                      ignition_conds=[vars(c).copy() for c in evals[0].conds],
                      watch_conds=[vars(c).copy() for c in watch_conds],
                      coil_entry_conds=coil_entry_conds, breakdown=breakdown)
        return row, signals, self.coil.watches.get(sym) if new_watch else None

    def _evaluate_short(self, sym: str, as_of: int, f: Features, b5: BarArrays, b15: BarArrays, b1h: BarArrays,
                        lm: LevelMap, regime: Regime | None, rstate: str, brackets: list[Bracket] | None,
                        ref_price: float, precision: int | None, dry_run: bool) -> list[Signal]:
        """Mirrored setups (short.enabled). Own cooldown and Coil watches, so the long side is
        exactly as without shorts."""
        cfg = self.cfg
        hr = headroom_down(lm, f.price, f.atr_1h_pct, cfg)
        evals = [eval_ignition_short(f, b5, hr, cfg, self.disabled)]
        if self.coil_enabled:
            self.coil_short.update(sym, as_of, f, b1h, self.disabled)
        if self.coil_enabled and as_of % 900_000 == 0:
            ce = self.coil_short.eval_entry(sym, f, b15, hr, self.disabled)
            if ce:
                evals.append(ce)
        key = f"{sym}:{SHORT}"
        if as_of - self.last_signal_ms.get(key, -10 ** 15) < cfg.signals.symbol_cooldown_min * 60_000:
            return []
        for ev in evals:
            score, breakdown = compute_score(ev.setup, f, rstate, hr, cfg, self.disabled)
            if ev.hard_pass and score >= cfg.score.min_entry:
                sig = self._make_signal(ev, sym, as_of, f, score, breakdown, regime, lm, ref_price, brackets, precision)
                if not dry_run:
                    self.last_signal_ms[key] = as_of
                    if ev.setup == COIL_SHORT:
                        self.coil_short.consume(sym)
                return [sig]
        return []


__all__ = ["SignalEngine", "Signal", "ScanRow", "ENTRY", "WATCH", "SKIP", "IGNITION", "COIL",
           "IGNITION_SHORT", "COIL_SHORT"]
