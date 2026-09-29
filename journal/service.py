"""Your manual trade journal (spec §8.2, §10.2, §14).

The buttons here RECORD what you did on WEEX - they never touch WEEX.

Each open trade carries a "shadow": the exit engine run on your logged entry / stop / targets.
It produces the action alerts (TP1, trail, TP2, exits). Your LOGGED stop is watched
separately: if price crosses it and you haven't logged an exit, the trade becomes
NEEDS_CONFIRMATION until you do. Trades logged late are replayed over historical 5m bars from
their entry time, so you see what the plan would have done so far.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select

from core.clock import TF_MS, floor_tf, last_closed_open, parse_local
from data.db import clean_json
from data.funding import funding_usd, qty_timeline
from data.trade_db import ManualTradeEventRow, ManualTradeRow, TradeEventRow
from exits.context import replay
from exits.engine import (EMA_EXIT, LIQUIDATION, MAX_HOLD, STOP, STOP_MOVE, TIME_STOP, TP1, TP2, ExitState,
                          make_params, new_state, on_bar_15m, on_prices, plan_legs)
from journal import pnl as jp
from journal.events import rebuild, stop_at
from journal.prefill import compute as prefill_compute
from journal.prefill import stop_warnings
from notify import actions
from notify.messages import dashboard_link, fmt_price
from paper.book import record_events
from plan.liquidation import link_size, liq_price_long
from plan.pnl import Leg, pnl_usd

log = logging.getLogger("journal")

OPEN, NEEDS, CLOSED = "OPEN", "NEEDS_CONFIRMATION", "CLOSED"
EPS = 1e-9


class JournalError(ValueError):
    pass


def _now() -> int:
    return int(time.time() * 1000)


def _num(body: dict, key: str, required: bool = True, positive: bool = True) -> float | None:
    v = body.get(key)
    if v in (None, ""):
        if required:
            raise JournalError(f"{key} is required")
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise JournalError(f"{key} must be a number") from None
    if positive and x <= 0:
        raise JournalError(f"{key} must be > 0")
    return x


class Journal:
    def __init__(self, eng):
        self.eng = eng
        self.cfg = eng.cfg
        self.db = eng.db
        self.shadows: dict[int, ExitState] = {}
        self._saved: dict[int, int] = {}

    # ------------------------------------------------------------------ helpers
    def _tz(self) -> str:
        return self.cfg.app.display_tz

    def _time(self, value, default_now: bool = True) -> int:
        if value in (None, ""):
            if default_now:
                return self.eng.clock.now_ms()
            raise JournalError("time is required")
        try:
            ts = parse_local(value, self._tz())
        except ValueError as e:
            raise JournalError(str(e)) from None
        if ts > self.eng.clock.now_ms() + 120_000:
            raise JournalError("time is in the future")
        return ts

    def signal(self, signal_id: str | None) -> dict | None:
        if not signal_id:
            return None
        row = self.db.get_signal(signal_id)
        if row is None:
            raise JournalError(f"signal {signal_id} not found")
        d = dict(row.data)
        d.update(signal_id=row.signal_id, telegram_msg_id=row.telegram_msg_id, symbol=row.symbol)
        return d

    def _events(self, s, trade_id: int) -> list:
        q = select(ManualTradeEventRow).where(ManualTradeEventRow.trade_id == trade_id).order_by(ManualTradeEventRow.id)
        return list(s.execute(q).scalars())

    def _add_event(self, s, trade_id: int, kind: str, ts: int, price=None, qty=None, old=None, new=None,
                   note: str = "") -> ManualTradeEventRow:
        ev = ManualTradeEventRow(trade_id=trade_id, ts=int(ts), logged_ms=_now(), kind=kind, price=price, qty=qty,
                                 old=clean_json(old) if old else None, new=clean_json(new) if new else None,
                                 note=note)
        s.add(ev)
        s.flush()
        return ev

    async def validate_price(self, sym: str, ts: int, price: float) -> str | None:
        """Warn (never block) if price is outside that minute's traded high/low."""
        if not self.cfg.journal.validate_prices:
            return None
        minute = floor_tf(ts, "1m")
        if minute + 60_000 > self.eng.clock.now_ms():
            return None   # current minute not closed yet - nothing to compare against
        try:
            bars = await self.eng.rest.history_klines(sym, "1m", minute, minute)
        except Exception as e:  # noqa: BLE001
            return f"could not check {price:g} against the market ({type(e).__name__})"
        bar = next((b for b in bars if b.t == minute), None)
        if bar is None:
            return None
        tol = 0.0005
        if price > bar.h * (1 + tol) or price < bar.l * (1 - tol):
            when = self.eng.clock.fmt(minute, with_date=True)[:-3]
            return f"price {price:g} is outside the market's range at {when} London ({bar.l:g} - {bar.h:g})"
        return None

    async def _notify(self, row: ManualTradeRow, text: str, key: str, event: str = "action") -> int | None:
        reply = row.thread_msg_id
        if row.signal_id:
            sig = self.db.get_signal(row.signal_id)
            reply = sig.telegram_msg_id if sig and sig.telegram_msg_id else reply
        return await self.eng.tg.send(text, dedupe_key=f"{row.trade_ref}:{key}", event=event,
                                      signal_id=row.signal_id, reply_to=reply)

    def _snapshot(self, row: ManualTradeRow) -> dict:
        return {"trade_ref": row.trade_ref, "signal_id": row.signal_id, "symbol": row.symbol,
                "entry_price": row.entry_price, "leverage": row.leverage, "liq_price": row.liq_price,
                "current_stop": row.current_stop, "tp1": row.tp1, "tp2": row.tp2, "warnings": row.warnings}

    def _link(self, row: ManualTradeRow) -> str:
        return f"{self.cfg.dashboard.base_url.rstrip('/')}/trade/{row.trade_ref}"

    def _new_shadow(self, row: ManualTradeRow) -> ExitState:
        # no TP1 given -> an unreachable level, so the plan just trails nothing and waits for exits
        params = make_params(row.entry_price, row.entry_ms, row.initial_stop, row.tp1 or row.entry_price * 1e9,
                             row.tp2, self.cfg, liq_price=row.liq_price)
        return new_state(params)

    async def _replay(self, row: ManualTradeRow, st: ExitState, rows: list, offline: bool) -> dict:
        """Advance shadow over closed 5m bars; watch your logged stop on the same path.
        Returns {"events": [...], "stop_hit": (ts, stop) | None}."""
        until = last_closed_open(self.eng.clock.now_ms(), "5m") + TF_MS["5m"]
        if until <= st.last_ts:
            return {"events": [], "stop_hit": None}
        b5, b15, mark5 = await self.eng.replay_bars(row.symbol, st.last_ts)
        rb = rebuild(row.qty, row.initial_stop, row.entry_ms, rows)
        timeline, legs = rb["stop_timeline"], rb["legs"]
        hit: list = []

        def watch(tc: int, lo: float, hi: float) -> None:
            if hit or tc <= row.entry_ms:
                return
            if any(l["ts"] <= tc for l in legs if l["kind"] == "CLOSE"):
                return
            stop = stop_at(timeline, tc)
            if lo <= stop:
                hit.append((tc, stop))

        evs = replay(st, b5, b15, self.cfg, until, mark5=mark5, on_bar=watch)
        return {"events": evs, "stop_hit": hit[0] if hit else None}

    # ------------------------------------------------------------------ create
    async def preview(self, body: dict) -> dict:
        sig = self.signal(body.get("signal_id"))
        sym = (sig or {}).get("symbol") or str(body.get("symbol", "")).upper()
        entry = _num(body, "entry_price")
        margin, lev, notional = link_size(_num(body, "margin"), _num(body, "leverage"),
                                          _num(body, "notional", required=False) or 0.0,
                                          body.get("edited", "margin"))
        pol = body.get("stop_policy") or str(self.cfg.trade.get("default_stop_policy", "L")).upper()
        custom = _num(body, "stop_price", required=False)
        out = prefill_compute(self.cfg, entry=entry, margin=margin, leverage=lev, brackets=self.eng.brackets.get(sym),
                              signal=sig, precision=self.eng.precision(sym), stop_policy=pol, custom_stop=custom)
        out.update(margin=margin, leverage=lev, symbol=sym)
        return clean_json(out)

    async def create(self, body: dict) -> dict:
        sig = self.signal(body.get("signal_id"))
        sym = (sig or {}).get("symbol") or str(body.get("symbol", "")).upper().strip()
        if not sym:
            raise JournalError("symbol is required")
        if sym not in self.eng.universe.exchange_symbols and not self.eng.store.has(sym):
            raise JournalError(f"{sym} is not a listed WEEX perpetual")
        entry = _num(body, "entry_price")
        entry_ms = self._time(body.get("entry_time"))
        max_age = self.cfg.journal.replay_max_days * 86_400_000
        margin, lev, notional = link_size(_num(body, "margin"), _num(body, "leverage"),
                                          _num(body, "notional", required=False) or 0.0,
                                          body.get("edited", "margin"))
        pol = body.get("stop_policy") or str(self.cfg.trade.get("default_stop_policy", "L")).upper()
        custom = _num(body, "stop_price", required=False)
        pre = prefill_compute(self.cfg, entry=entry, margin=margin, leverage=lev, brackets=self.eng.brackets.get(sym),
                              signal=sig, precision=self.eng.precision(sym), stop_policy=pol, custom_stop=custom)
        stop = custom if custom is not None else pre["stop"]
        if stop is None:
            raise JournalError("no stop: choose L, S (when available) or enter a custom stop")
        if stop >= entry:
            raise JournalError("the stop must be below the entry for a long")
        tp1 = _num(body, "tp1", required=False) or pre["tp1"]
        tp2 = _num(body, "tp2", required=False) if body.get("tp2") not in (None, "") else pre["tp2"]
        entry_order = body.get("entry_order") or self.cfg.journal.default_entry_order
        qty = notional / entry
        liq = liq_price_long(entry, qty, margin, self.eng.brackets[sym]) if self.eng.brackets.get(sym) else pre["liq_price"]
        warnings = [w for w in pre["warnings"] if "stop" not in w] + stop_warnings(self.cfg, stop, entry, liq)
        vw = await self.validate_price(sym, entry_ms, entry)
        if vw:
            warnings.append(vw)
        if self.eng.clock.now_ms() - entry_ms > max_age:
            warnings.append(f"entry is more than {self.cfg.journal.replay_max_days} days ago: plan replay skipped")

        now = _now()
        with self.db.session() as s:
            row = ManualTradeRow(
                signal_id=(sig or {}).get("signal_id"), source="signal" if sig else "manual_own", symbol=sym,
                status=OPEN, entry_ms=entry_ms, entry_price=entry, entry_order=entry_order, margin=margin,
                leverage=lev, notional=notional, qty=qty, qty_open=qty, stop_policy=pol, initial_stop=stop,
                current_stop=stop, tp1=tp1, tp2=tp2, liq_price=liq,
                risk_usd=jp.risk_usd(self.cfg, entry, qty, entry_order, stop), notes=str(body.get("notes") or "")[:2000],
                warnings=warnings, legs=[], shadow={}, entry_gap_pct=pre.get("entry_gap_pct"),
                created_ms=now, updated_ms=now)
            s.add(row)
            s.flush()
            row.trade_ref = f"M-{row.id:04d}"
            self._add_event(s, row.id, "OPEN", entry_ms, entry, qty, new={
                "entry_price": entry, "margin": margin, "leverage": lev, "notional": notional, "stop": stop,
                "stop_policy": pol, "tp1": tp1, "tp2": tp2, "entry_order": entry_order, "warnings": warnings})
            st = self._new_shadow(row)
            row.shadow = clean_json(st.to_dict())
            s.commit()
            trade_id = row.id
        self.shadows[trade_id] = st

        # late logging: replay the plan from the entry time
        summary: list[str] = []
        if self.eng.clock.now_ms() - entry_ms > TF_MS["5m"] and self.eng.clock.now_ms() - entry_ms <= max_age:
            summary = await self._late_replay(trade_id)
        with self.db.session() as s:
            row = s.get(ManualTradeRow, trade_id)
            p = self.eng.precision(sym)
            msg_id = await self._notify(row, actions.tracking(self._snapshot(row), p, self._link(row), summary),
                                        "tracking", "tracking")
            if msg_id and not row.signal_id:
                row.thread_msg_id = msg_id
            chase = ((sig or {}).get("plan") or {}).get("chase_limit")
            if chase and entry > float(chase):
                await self._notify(row, actions.chase(self._snapshot(row), float(chase), p), "chase")
            s.commit()
        return self.detail(trade_id)

    async def _late_replay(self, trade_id: int, offline: bool = False) -> list[str]:
        with self.db.session() as s:
            row = s.get(ManualTradeRow, trade_id)
            rows = self._events(s, trade_id)
            st = self.shadows.get(trade_id) or ExitState.from_dict(row.shadow)
            res = await self._replay(row, st, rows, offline)
            record_events(self.db, "manual", trade_id, res["events"])
            row.shadow = clean_json(st.to_dict())
            row.replayed = True
            items = self._summarise(row, st, res["events"])
            flagged = []
            if res["stop_hit"] and row.status == OPEN:
                ts, stop = res["stop_hit"]
                flagged.append(f"price hit your SL {fmt_price(stop, self.eng.precision(row.symbol))} at "
                               f"{self.eng.clock.fmt(ts)} London - log your exit")
            if offline and any(e.kind in (TP1, TP2, STOP, EMA_EXIT, TIME_STOP, MAX_HOLD, LIQUIDATION)
                               for e in res["events"]):
                flagged.append("targets/exits were reached while offline - check the plan timeline")
            if flagged:
                self._flag(s, row, "; ".join(flagged))
            s.commit()
        self.shadows[trade_id] = st
        return items + flagged

    def _summarise(self, row: ManualTradeRow, st: ExitState, evs) -> list[str]:
        p = self.eng.precision(row.symbol)
        f = self.eng.clock.fmt
        out = []
        for e in evs:
            if e.kind == TP1:
                out.append(f"TP1 reached at {f(e.ts)}: close {e.fraction * 100:.0f}% at ~{fmt_price(e.price, p)}")
            elif e.kind == TP2:
                out.append(f"TP2 reached at {f(e.ts)}: close {e.fraction * 100:.0f}% at ~{fmt_price(e.price, p)}")
            elif e.kind in (STOP, EMA_EXIT, TIME_STOP, MAX_HOLD, LIQUIDATION):
                out.append(f"plan exit ({e.kind.replace('_', ' ').lower()}) at {f(e.ts)} ~{fmt_price(e.price, p)}")
        if not st.closed:
            note = " (breakeven + fees)" if st.tp1_ms and abs(st.stop - st.p.be_stop) < 1e-12 else ""
            out.append(f"SL should now be at {fmt_price(st.stop, p)}{note}")
        return out

    def _flag(self, s, row: ManualTradeRow, reason: str) -> None:
        if row.status == CLOSED:
            return
        row.status = NEEDS
        row.needs_confirmation = reason
        row.updated_ms = _now()
        self._add_event(s, row.id, "NEEDS_CONFIRMATION", self.eng.clock.now_ms(), note=reason)

    # ------------------------------------------------------------------ logging actions
    def _load(self, s, trade_ref: str) -> ManualTradeRow:
        row = s.execute(select(ManualTradeRow).where(ManualTradeRow.trade_ref == trade_ref)).scalar_one_or_none()
        if row is None:
            raise JournalError(f"trade {trade_ref} not found")
        return row

    async def _apply_rebuild(self, s, row: ManualTradeRow) -> None:
        rb = rebuild(row.qty, row.initial_stop, row.entry_ms, self._events(s, row.id))
        row.legs = clean_json(rb["legs"])
        row.qty_open = rb["qty_open"]
        row.current_stop = rb["current_stop"]
        row.updated_ms = _now()
        if row.qty_open <= row.qty * 1e-6:
            row.qty_open = 0.0
            await self._finalize(s, row)
        elif row.status == CLOSED:           # an exit was voided/edited: trade is open again
            row.status, row.closed_ms, row.net_pnl = OPEN, None, None

    async def log_exit(self, trade_ref: str, body: dict, full: bool) -> dict:
        with self.db.session() as s:
            row = self._load(s, trade_ref)
            if row.qty_open <= 0:
                raise JournalError("this trade is already fully closed")
            price = _num(body, "price")
            ts = self._time(body.get("time"))
            if ts < row.entry_ms:
                raise JournalError("exit time is before the entry time")
            if full:
                qty = row.qty_open
            elif body.get("qty") not in (None, ""):
                qty = min(_num(body, "qty"), row.qty_open)
            else:
                pct = _num(body, "pct")
                if pct > 100:
                    raise JournalError("pct must be <= 100")
                qty = min(row.qty * pct / 100, row.qty_open)
            order = body.get("order") or self.cfg.journal.default_exit_order
            warn = await self.validate_price(row.symbol, ts, price)
            kind = "CLOSE" if full or qty >= row.qty_open * (1 - 1e-9) else "PARTIAL_CLOSE"
            self._add_event(s, row.id, kind, ts, price, qty, new={"order": order}, note=warn or "")
            await self._apply_rebuild(s, row)
            s.commit()
            ref = row.trade_ref
        out = self.detail_by_ref(ref)
        out["warning"] = warn
        return out

    async def log_stop(self, trade_ref: str, body: dict) -> dict:
        with self.db.session() as s:
            row = self._load(s, trade_ref)
            price = _num(body, "price")
            ts = self._time(body.get("time"))
            if ts < row.entry_ms:
                raise JournalError("stop-move time is before the entry time")
            old = row.current_stop   # a stop level is not a traded price: nothing to validate
            self._add_event(s, row.id, "STOP_MOVE", ts, price, None, old={"stop": old}, new={"stop": price})
            await self._apply_rebuild(s, row)
            extra = stop_warnings(self.cfg, row.current_stop, row.entry_price, row.liq_price) if row.liq_price else []
            s.commit()
        out = self.detail_by_ref(trade_ref)
        out["warning"] = "; ".join(extra) or None
        return out

    async def edit_event(self, trade_ref: str, body: dict) -> dict:
        """Correct or withdraw one of your logged events (audit trail keeps everything)."""
        with self.db.session() as s:
            row = self._load(s, trade_ref)
            eid = int(body.get("event_id", 0))
            target = s.get(ManualTradeEventRow, eid)
            if target is None or target.trade_id != row.id or target.kind not in ("PARTIAL_CLOSE", "CLOSE", "STOP_MOVE"):
                raise JournalError("event not found")
            if body.get("void"):
                self._add_event(s, row.id, "VOID", self.eng.clock.now_ms(), new={"event_id": eid},
                                note=str(body.get("note") or "withdrawn"))
            else:
                new: dict = {"event_id": eid}
                if body.get("price") not in (None, ""):
                    new["price"] = _num(body, "price")
                if body.get("time") not in (None, ""):
                    new["ts"] = self._time(body["time"])
                if body.get("qty") not in (None, ""):
                    new["qty"] = _num(body, "qty")
                if len(new) == 1:
                    raise JournalError("nothing to change")
                old = {"price": target.price, "ts": target.ts, "qty": target.qty}
                self._add_event(s, row.id, "EDIT", self.eng.clock.now_ms(), old=old, new=new,
                                note=str(body.get("note") or ""))
            await self._apply_rebuild(s, row)
            s.commit()
        return self.detail_by_ref(trade_ref)

    async def edit_trade(self, trade_ref: str, body: dict) -> dict:
        """Edit entry fields (audit trail keeps old -> new). Entry/size/stop/TP changes re-run the plan."""
        fields = {}
        with self.db.session() as s:
            row = self._load(s, trade_ref)
            old = {}
            if body.get("entry_price") not in (None, ""):
                fields["entry_price"] = _num(body, "entry_price")
            if body.get("entry_time") not in (None, ""):
                fields["entry_ms"] = self._time(body["entry_time"])
            if any(body.get(k) not in (None, "") for k in ("margin", "leverage", "notional")):
                m, l, n = link_size(_num(body, "margin", required=False) or row.margin,
                                    _num(body, "leverage", required=False) or row.leverage,
                                    _num(body, "notional", required=False) or row.notional,
                                    body.get("edited", "margin"))
                fields.update(margin=m, leverage=l, notional=n)
            for k in ("initial_stop", "tp1", "tp2"):
                if body.get(k) not in (None, ""):
                    fields[k] = _num(body, k)
            if "notes" in body:
                fields["notes"] = str(body.get("notes") or "")[:2000]
            if body.get("entry_order") in ("market", "limit"):
                fields["entry_order"] = body["entry_order"]
            if not fields:
                raise JournalError("nothing to change")
            for k, v in fields.items():
                old[k] = getattr(row, k)
                setattr(row, k, v)
            if row.initial_stop >= row.entry_price:
                raise JournalError("the stop must be below the entry for a long")
            row.qty = row.notional / row.entry_price
            if self.eng.brackets.get(row.symbol):
                row.liq_price = liq_price_long(row.entry_price, row.qty, row.margin, self.eng.brackets[row.symbol])
            row.risk_usd = jp.risk_usd(self.cfg, row.entry_price, row.qty, row.entry_order, row.initial_stop)
            if row.signal_id:
                sig = self.signal(row.signal_id)
                ref = float(sig["plan"]["ref_entry"])
                row.entry_gap_pct = (row.entry_price / ref - 1) * 100
            self._add_event(s, row.id, "EDIT", self.eng.clock.now_ms(), old=old, new=fields,
                            note=str(body.get("note") or "trade edited"))
            await self._apply_rebuild(s, row)
            plan_changed = any(k in fields for k in ("entry_price", "entry_ms", "initial_stop", "tp1", "tp2",
                                                     "margin", "leverage", "notional"))
            if plan_changed:
                st = self._new_shadow(row)
                row.shadow = clean_json(st.to_dict())
                self.shadows[row.id] = st
            s.commit()
            trade_id = row.id
        if plan_changed and trade_id in self.shadows:
            await self._late_replay(trade_id)
        return self.detail_by_ref(trade_ref)

    async def confirm_open(self, trade_ref: str, body: dict) -> dict:
        """You checked WEEX: the position is still open (e.g. you had moved the SL). Clears the flag."""
        with self.db.session() as s:
            row = self._load(s, trade_ref)
            if row.status != NEEDS:
                raise JournalError("trade is not waiting for confirmation")
            self._add_event(s, row.id, "CONFIRMED_OPEN", self.eng.clock.now_ms(), note=str(body.get("note") or ""))
            row.status, row.needs_confirmation = OPEN, None
            s.commit()
        return self.detail_by_ref(trade_ref)

    async def close_all(self, items: list[dict]) -> dict:
        done, errors = [], []
        for it in items:
            try:
                await self.log_exit(str(it.get("trade_ref")), it, full=True)
                done.append(it.get("trade_ref"))
            except JournalError as e:
                errors.append(f"{it.get('trade_ref')}: {e}")
        return {"closed": done, "errors": errors}

    async def _finalize(self, s, row: ManualTradeRow) -> None:
        legs = list(row.legs)
        exit_ms = max(int(l["ts"]) for l in legs)
        settlements = await self.eng.funding.settlements(row.symbol, row.entry_ms, exit_ms)
        fund = funding_usd(settlements, row.entry_ms, qty_timeline(row.qty, legs), row.entry_price)
        net = jp.confirmed_pnl(self.cfg, row.entry_price, row.qty, row.entry_order, legs, fund)
        row.status, row.closed_ms, row.needs_confirmation = CLOSED, exit_ms, None
        row.net_pnl, row.funding_usd = net, fund
        row.r_multiple = net / row.risk_usd if row.risk_usd else None
        st = self.shadows.get(row.id) or (ExitState.from_dict(row.shadow) if row.shadow else None)
        if st is not None:
            pl = [Leg(p, f, k) for p, f, k in plan_legs(st)]
            rest = 1.0 - sum(l.fraction for l in pl)
            if rest > EPS:
                pl.append(Leg(float(legs[-1]["price"]), rest, "market"))
            row.plan_pnl = pnl_usd(row.entry_price, row.qty, pl, self.cfg, funding_usd=fund, entry_is_fill=True)
        self.shadows.pop(row.id, None)

    # ------------------------------------------------------------------ live tracking
    def open_symbols(self) -> set[str]:
        with self.db.session() as s:
            q = select(ManualTradeRow.symbol).where(ManualTradeRow.status != CLOSED)
            return {r[0] for r in s.execute(q)}

    def _open_rows(self, s, sym: str | None = None) -> list[ManualTradeRow]:
        q = select(ManualTradeRow).where(ManualTradeRow.status != CLOSED)
        if sym:
            q = q.where(ManualTradeRow.symbol == sym)
        return list(s.execute(q).scalars())

    async def on_sample(self, sym: str, ts: int, trig_low: float, high: float, last: float,
                        mark_low: float | None) -> None:
        with self.db.session() as s:
            for row in self._open_rows(s, sym):
                st = self.shadows.get(row.id)
                if st is None:
                    continue
                evs = on_prices(st, ts, trig_low, high, last, mark_low=mark_low)
                await self._alerts(s, row, st, evs, user_low=trig_low)
            s.commit()

    async def on_15m(self, sym: str, ts: int, ctx: tuple) -> None:
        with self.db.session() as s:
            for row in self._open_rows(s, sym):
                st = self.shadows.get(row.id)
                if st is not None:
                    await self._alerts(s, row, st, on_bar_15m(st, ts, *ctx), user_low=None)
            s.commit()

    async def _alerts(self, s, row: ManualTradeRow, st: ExitState, evs, user_low: float | None) -> None:
        p = self.eng.precision(row.symbol)
        snap = self._snapshot(row)
        now = self.eng.clock.now_ms()
        user_hit = user_low is not None and row.status == OPEN and row.qty_open > 0 and user_low <= row.current_stop
        if evs:
            record_events(self.db, "manual", row.id, evs)
        for e in evs:
            key = f"{e.kind}:{e.ts}"
            if e.kind == TP1:
                await self._notify(row, actions.tp1(snap, e.fraction * 100, e.price, st.p.be_stop, p), key)
            elif e.kind == TP2:
                runner = st.remaining * 100
                await self._notify(row, actions.tp2(snap, e.fraction * 100, e.price, runner, p), key)
            elif e.kind == STOP_MOVE and st.tp1_ms and e.ts != st.tp1_ms:
                gain = (e.stop_after / row.current_stop - 1) * 100
                interval = self.cfg.journal.trail_notify_min_interval_min * 60_000
                if gain >= self.cfg.journal.trail_notify_min_pct and now - (row.last_trail_alert_ms or 0) >= interval:
                    await self._notify(row, actions.trail(snap, e.stop_after, p), key)
                    row.last_trail_alert_ms = now
            elif e.kind == LIQUIDATION:
                await self._notify(row, actions.liquidation(snap, e.price, p), key)
                self._flag(s, row, "mark price reached the liquidation price")
            elif e.kind in (EMA_EXIT, TIME_STOP, MAX_HOLD):
                await self._notify(row, actions.close_remaining(snap, e.kind, p), key)
            elif e.kind == STOP and not user_hit:
                # the plan's (trailed) stop was hit but your logged stop is lower
                await self._notify(row, actions.close_remaining(snap, STOP, p, stop=e.price), key)
        if user_hit:
            await self._notify(row, actions.stop_hit(snap, row.current_stop, p), f"stop_hit:{row.current_stop}")
            self._flag(s, row, f"price hit your SL {row.current_stop:g} at {self.eng.clock.fmt(now)} London")
        if evs or now - self._saved.get(row.id, 0) > 60_000:
            row.shadow = clean_json(st.to_dict())
            self._saved[row.id] = now

    async def load_and_catch_up(self) -> list[str]:
        """Startup: load open trades, replay the time the app was offline. Returns summary lines."""
        lines = []
        with self.db.session() as s:
            rows = self._open_rows(s)
            ids = [(r.id, r.trade_ref, r.symbol) for r in rows]
            for r in rows:
                self.shadows[r.id] = ExitState.from_dict(r.shadow) if r.shadow else self._new_shadow(r)
        for tid, ref, sym in ids:
            try:
                items = await self._late_replay(tid, offline=True)
            except Exception as e:  # noqa: BLE001
                log.exception("catch-up failed for %s", ref)
                items = [f"catch-up failed: {e}"]
            with self.db.session() as s:
                row = s.get(ManualTradeRow, tid)
                mark = self.eng.premium.mark(sym)
                mtm = jp.mark_to_market(self.cfg, row.entry_price, row.qty, row.entry_order, row.legs, mark) \
                    if mark else None
                p = self.eng.precision(sym)
                flag = " ⚠️ NEEDS CONFIRMATION" if row.status == NEEDS else ""
                lines.append(f"{ref} {sym} {row.signal_id or 'own'}: SL {fmt_price(row.current_stop, p)}, "
                             f"suggested {fmt_price(self.shadows[tid].stop, p)}"
                             + (f", P&L ~{mtm:+.2f}$" if mtm is not None else "") + flag)
                if row.status == NEEDS and row.needs_confirmation:
                    await self._notify(row, actions.offline_crossing(self._snapshot(row), [row.needs_confirmation]),
                                       f"offline:{row.updated_ms}")
        return lines

    # ------------------------------------------------------------------ views
    def price(self, sym: str) -> float | None:
        return self.eng.tape.last_price.get(sym) or self.eng.premium.mark(sym)

    def pending(self, row: ManualTradeRow, st: ExitState | None) -> list[str]:
        """Plan events you still need to act on / confirm (spec §8.2 late logging)."""
        if row.status == CLOSED or st is None:
            return []
        p = self.eng.precision(row.symbol)
        f = self.eng.clock.fmt
        legs = row.legs or []
        out = []
        if row.status == NEEDS and row.needs_confirmation:
            out.append(row.needs_confirmation)
        if st.tp1_ms and not any(l["ts"] >= st.tp1_ms - 300_000 for l in legs):
            out.append(f"TP1 reached at {f(st.tp1_ms)}: close {st.p.tp1_frac * 100:.0f}% at ~{fmt_price(st.p.tp1, p)}")
        if st.tp2_ms and sum(1 for l in legs if l["ts"] >= st.tp2_ms - 300_000) < 1:
            out.append(f"TP2 reached at {f(st.tp2_ms)}: close {st.p.tp2_frac * 100:.0f}% at ~{fmt_price(st.p.tp2, p)}")
        if st.closed:
            out.append(f"plan exited ({(st.exit_reason or '').replace('_', ' ').lower()}) at {f(st.exit_ms)}: "
                       f"close the remaining {row.qty_open / row.qty * 100:.0f}% and log it")
        elif st.stop > row.current_stop * (1 + 1e-6):
            out.append(f"move SL to {fmt_price(st.stop, p)} (you logged {fmt_price(row.current_stop, p)})")
        return out

    def _view(self, row: ManualTradeRow) -> dict:
        st = self.shadows.get(row.id) or (ExitState.from_dict(row.shadow) if row.shadow else None)
        px = self.price(row.symbol)
        d = {c: getattr(row, c) for c in (
            "id", "trade_ref", "signal_id", "source", "symbol", "status", "entry_ms", "entry_price", "entry_order",
            "margin", "leverage", "notional", "qty", "qty_open", "stop_policy", "initial_stop", "current_stop",
            "tp1", "tp2", "liq_price", "risk_usd", "notes", "warnings", "legs", "net_pnl", "funding_usd",
            "r_multiple", "plan_pnl", "entry_gap_pct", "needs_confirmation", "created_ms", "closed_ms")}
        d["precision"] = self.eng.precision(row.symbol)
        d["price"] = px
        d["confirmed_pnl"] = jp.confirmed_pnl(self.cfg, row.entry_price, row.qty, row.entry_order, row.legs or [])
        d["mtm_pnl"] = (jp.mark_to_market(self.cfg, row.entry_price, row.qty, row.entry_order, row.legs or [], px)
                        if px and row.status != CLOSED else None)
        d["suggested_stop"] = st.stop if st and not st.closed else None
        d["plan_phase"] = st.phase if st else None
        d["plan_closed"] = bool(st and st.closed)
        pend = self.pending(row, st)
        d["pending"] = pend
        d["next_action"] = pend[0] if pend else (
            f"hold - SL {fmt_price(row.current_stop, d['precision'])}"
            + (f", TP1 {fmt_price(row.tp1, d['precision'])}" if row.tp1 and not (st and st.tp1_ms) else "")
            if row.status != CLOSED else "closed")
        d["adherence_usd"] = (row.net_pnl - row.plan_pnl) if row.net_pnl is not None and row.plan_pnl is not None else None
        d["link"] = f"/trade/{row.trade_ref}"
        return d

    def list(self, include_closed: int = 100) -> dict:
        with self.db.session() as s:
            open_rows = self._open_rows(s)
            closed = list(s.execute(select(ManualTradeRow).where(ManualTradeRow.status == CLOSED)
                                    .order_by(ManualTradeRow.closed_ms.desc()).limit(include_closed)).scalars())
            return clean_json({"open": [self._view(r) for r in open_rows], "closed": [self._view(r) for r in closed]})

    def detail(self, trade_id: int) -> dict:
        with self.db.session() as s:
            row = s.get(ManualTradeRow, trade_id)
            if row is None:
                raise JournalError("trade not found")
            d = self._view(row)
            d["audit"] = [{"id": e.id, "ts": e.ts, "logged_ms": e.logged_ms, "kind": e.kind, "price": e.price,
                           "qty": e.qty, "old": e.old, "new": e.new, "note": e.note} for e in self._events(s, row.id)]
            d["effective"] = rebuild(row.qty, row.initial_stop, row.entry_ms, self._events(s, row.id))["events"]
            q = select(TradeEventRow).where(TradeEventRow.trade_type == "manual", TradeEventRow.trade_id == row.id) \
                .order_by(TradeEventRow.ts, TradeEventRow.id)
            d["plan_events"] = [{"ts": e.ts, "kind": e.kind, "price": e.price, "fraction": e.fraction,
                                 "stop_after": e.stop_after, "note": e.note} for e in s.execute(q).scalars()]
            return clean_json(d)

    def detail_by_ref(self, trade_ref: str) -> dict:
        with self.db.session() as s:
            row = self._load(s, trade_ref)
            tid = row.id
        return self.detail(tid)

    def holding(self, sym: str) -> bool:
        return sym in self.open_symbols()
