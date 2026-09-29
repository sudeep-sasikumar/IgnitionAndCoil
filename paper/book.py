"""Baseline paper trading (spec §8.1): every ENTRY signal - sent or suppressed - opens one
paper trade per stop policy (L, and S when valid) at default size, whatever you do.

Fill model: reference entry + slippage, fees per leg, funding at each settlement held
through. Stops trigger on the configured price (mark/last). Exits come from the shared exit
engine. Records every exit leg, MFE/MAE, time to each target, exit reason, regime, setup,
score and policy.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from data.db import clean_json
from data.funding import funding_usd, qty_timeline
from data.trade_db import PaperTradeRow, TradeEventRow
from exits.engine import MARKET_EXITS, STOP_MOVE, TP1, TP2, ExitEvent, ExitState, make_params, new_state, \
    on_bar_15m, on_prices
from plan.pnl import Leg, entry_fill, pnl_usd

log = logging.getLogger("paper")

LEG_KIND = {TP1: "tp", TP2: "tp"}


def leg_kind(ev_kind: str) -> str:
    return LEG_KIND.get(ev_kind, "market" if ev_kind in MARKET_EXITS else "stop")


@dataclass
class PaperPos:
    id: int
    signal_id: str
    policy: str
    symbol: str
    entry_ref: float
    entry_fill: float
    qty: float
    risk_usd: float
    state: ExitState
    legs: list = field(default_factory=list)
    dirty_ms: int = 0


class PaperBook:
    def __init__(self, cfg, db, funding):
        self.cfg, self.db, self.funding = cfg, db, funding
        self.open: dict[int, PaperPos] = {}

    # ---- lifecycle ---------------------------------------------------------
    def load_open(self) -> None:
        with self.db.session() as s:
            for r in s.execute(select(PaperTradeRow).where(PaperTradeRow.status == "OPEN")).scalars():
                self.open[r.id] = PaperPos(r.id, r.signal_id, r.policy, r.symbol, r.entry_ref, r.entry_fill, r.qty,
                                           r.risk_usd, ExitState.from_dict(r.state), list(r.legs or []))
        log.info("paper: %d open trades loaded", len(self.open))

    def symbols(self) -> set[str]:
        return {p.symbol for p in self.open.values()}

    def open_for_signal(self, sig: dict) -> list[int]:
        """Open L (and S if valid) paper trades for a recorded signal. Idempotent per policy."""
        if not self.cfg.paper.enabled:
            return []
        plan = sig["plan"]
        ref = float(plan["ref_entry"])
        qty = float(plan["qty"])
        fill = entry_fill(ref, self.cfg)
        entry_ms = int(sig["bar_close_ms"])
        ids = []
        for pol_key in ("stop_l", "stop_s"):
            sp = plan[pol_key]
            if sp.get("price") is None:
                continue
            stop = float(sp["price"])
            params = make_params(fill, entry_ms, stop, float(plan["tp1"]), plan.get("tp2"), self.cfg,
                                 liq_price=plan.get("liq_price"))
            st = new_state(params)
            risk = abs(pnl_usd(ref, qty, [Leg(stop, 1.0, "stop")], self.cfg))
            row = PaperTradeRow(
                signal_id=sig["signal_id"], policy=sp["policy"], symbol=sig["symbol"], setup=sig["setup"],
                score=int(sig["score"]), regime=sig.get("regime", {}).get("state", "?"), session=sig["session"],
                tags=list(sig.get("tags") or []), suppressed_reason=sig.get("suppressed_reason"), status="OPEN",
                entry_ms=entry_ms, entry_ref=ref, entry_fill=fill, qty=qty, notional=float(plan["notional"]),
                margin=float(plan["margin"]), leverage=float(plan["leverage"]), initial_stop=stop,
                tp1=float(plan["tp1"]), tp2=plan.get("tp2"), liq_price=plan.get("liq_price"), risk_usd=risk,
                legs=[], state=clean_json(st.to_dict()), config_hash=self.cfg.hash, updated_ms=_now())
            with self.db.session() as s:
                exists = s.execute(select(PaperTradeRow.id).where(
                    PaperTradeRow.signal_id == row.signal_id, PaperTradeRow.policy == row.policy)).first()
                if exists:
                    continue
                s.add(row)
                s.commit()
                self.open[row.id] = PaperPos(row.id, row.signal_id, row.policy, row.symbol, ref, fill, qty, risk, st)
                ids.append(row.id)
        return ids

    # ---- price processing ----------------------------------------------------
    async def on_sample(self, sym: str, ts: int, trig_low: float, high: float, last: float,
                        mark_low: float | None) -> list[dict]:
        closed = []
        for pos in [p for p in self.open.values() if p.symbol == sym]:
            evs = on_prices(pos.state, ts, trig_low, high, last, mark_low=mark_low)
            closed += await self._apply(pos, evs)
        return closed

    async def on_15m(self, sym: str, ts: int, ctx: tuple) -> list[dict]:
        closed = []
        for pos in [p for p in self.open.values() if p.symbol == sym]:
            closed += await self._apply(pos, on_bar_15m(pos.state, ts, *ctx))
        return closed

    async def apply_events(self, pos: PaperPos, evs: list[ExitEvent]) -> list[dict]:
        return await self._apply(pos, evs)

    async def _apply(self, pos: PaperPos, evs: list[ExitEvent]) -> list[dict]:
        if not evs:
            if _now() - pos.dirty_ms > 60_000:   # persist MFE/MAE progress now and then
                self._save(pos)
            return []
        record_events(self.db, "paper", pos.id, evs)
        for e in evs:
            if e.kind != STOP_MOVE:
                pos.legs.append({"ts": e.ts, "price": e.price, "fraction": e.fraction,
                                 "qty": pos.qty * e.fraction, "kind": leg_kind(e.kind), "reason": e.kind})
        if pos.state.closed:
            return [await self._finalize(pos)]
        self._save(pos)
        return []

    def _save(self, pos: PaperPos, **extra) -> None:
        with self.db.session() as s:
            r = s.get(PaperTradeRow, pos.id)
            r.state = clean_json(pos.state.to_dict())
            r.legs = clean_json(list(pos.legs))
            st = pos.state
            r.mfe_pct = (st.highest / pos.entry_fill - 1) * 100
            r.mae_pct = (st.lowest / pos.entry_fill - 1) * 100
            r.tp1_ms, r.tp2_ms = st.tp1_ms, st.tp2_ms
            r.updated_ms = _now()
            for k, v in extra.items():
                setattr(r, k, v)
            s.commit()
        pos.dirty_ms = _now()

    async def _finalize(self, pos: PaperPos) -> dict:
        st = pos.state
        settlements = await self.funding.settlements(pos.symbol, st.p.entry_ms, st.exit_ms)
        fund = funding_usd(settlements, st.p.entry_ms, qty_timeline(pos.qty, pos.legs), pos.entry_ref)
        legs = [Leg(l["price"], l["fraction"], l["kind"]) for l in pos.legs]
        net = pnl_usd(pos.entry_ref, pos.qty, legs, self.cfg, funding_usd=fund)
        r = net / pos.risk_usd if pos.risk_usd else None
        self._save(pos, status="CLOSED", exit_ms=st.exit_ms, exit_reason=st.exit_reason, net_pnl=net,
                   funding_usd=fund, r_multiple=r)
        self.open.pop(pos.id, None)
        log.info("paper %s %s closed %s net %.2f (%.2fR)", pos.signal_id, pos.policy, st.exit_reason, net, r or 0)
        return {"id": pos.id, "signal_id": pos.signal_id, "policy": pos.policy, "symbol": pos.symbol,
                "exit_reason": st.exit_reason, "net_pnl": net, "r": r, "entry_ms": st.p.entry_ms,
                "exit_ms": st.exit_ms, "legs": [l["reason"] for l in pos.legs]}


def record_events(db, trade_type: str, trade_id: int, evs: list[ExitEvent]) -> None:
    """Store exit-engine events; each in its own savepoint so a duplicate (same event replayed
    twice, e.g. after a restart) is skipped without losing the others."""
    with db.session() as s:
        for e in evs:
            try:
                with s.begin_nested():
                    s.add(TradeEventRow(trade_type=trade_type, trade_id=trade_id, ts=e.ts, kind=e.kind,
                                        price=e.price, fraction=e.fraction, stop_before=e.stop_before,
                                        stop_after=e.stop_after, note=e.note))
            except IntegrityError:
                pass
        s.commit()


def _now() -> int:
    return int(time.time() * 1000)
