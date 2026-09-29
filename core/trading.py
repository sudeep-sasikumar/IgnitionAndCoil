"""Trade runtime (M4): live tracking of paper + logged trades, 15m exit-engine hooks, restart
catch-up, paper-result lines, bedtime reminders, daily summary, DB backups, trade commands."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from core.clock import TF_MS, last_closed_open
from exits.context import ctx_15m, replay
from notify.messages import fmt_price
from stats import service as stats_service

log = logging.getLogger("trading")


def fmt_dur(ms: int) -> str:
    m = max(0, int(ms)) // 60_000
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


class Trading:
    def __init__(self, eng):
        self.eng = eng
        self.cfg = eng.cfg
        self._last_bedtime_ms = 0

    # ---- symbols -------------------------------------------------------------
    def symbols(self) -> set[str]:
        return self.eng.paper.symbols() | self.eng.journal.open_symbols()

    # ---- live tracking ---------------------------------------------------------
    async def track_loop(self) -> None:
        eng = self.eng
        use_mark = self.cfg.trade.stop_trigger_price == "mark"
        while True:
            await asyncio.sleep(self.cfg.exits.tick_s)
            if not eng.healthy:
                continue   # never act on stale prices
            ts = eng.clock.now_ms()
            for sym in self.symbols():
                w = eng.tape.drain(sym)
                if w is None:
                    continue
                lo, hi, last = w
                mark = eng.premium.mark(sym)
                trig = mark if (use_mark and mark) else lo   # stop trigger price per config
                try:
                    closed = await eng.paper.on_sample(sym, ts, trig, hi, last, mark)
                    await self.paper_updates(closed)
                    await eng.journal.on_sample(sym, ts, trig, hi, last, mark)
                except Exception:  # noqa: BLE001
                    log.exception("tracking %s failed", sym)

    async def on_15m(self, ts: int) -> None:
        eng = self.eng
        for sym in self.symbols():
            b15 = eng.store.series(sym, "15m").arrays().upto(ts)
            if not len(b15) or int(b15.tc[-1]) != ts:
                continue
            ctx = ctx_15m(b15, self.cfg)
            try:
                await self.paper_updates(await eng.paper.on_15m(sym, ts, ctx))
                await eng.journal.on_15m(sym, ts, ctx)
            except Exception:  # noqa: BLE001
                log.exception("15m exit update %s failed", sym)

    async def catch_up_paper(self) -> None:
        """After a restart: advance open paper trades over the bars that closed while offline."""
        eng = self.eng
        until = last_closed_open(eng.clock.now_ms(), "5m") + TF_MS["5m"]
        closed = []
        for pos in list(eng.paper.open.values()):
            try:
                b5, b15, mark5 = await eng.replay_bars(pos.symbol, pos.state.last_ts)
                evs = replay(pos.state, b5, b15, self.cfg, until, mark5=mark5)
                closed += await eng.paper.apply_events(pos, evs)
            except Exception:  # noqa: BLE001
                log.exception("paper catch-up failed for %s", pos.signal_id)
        await self.paper_updates(closed)

    async def paper_updates(self, closed: list[dict]) -> None:
        eng = self.eng
        if not closed or self.cfg.notifications.paper_updates != "summary" or eng.db.get_state("paper_muted", False):
            return
        for c in closed:
            sig = eng.db.get_signal(c["signal_id"])
            reply = sig.telegram_msg_id if sig else None
            path = "+".join(dict.fromkeys(c["legs"])) or c["exit_reason"]
            r = f" ({c['r']:+.2f}R)" if c.get("r") is not None else ""
            text = (f"📄 Paper {c['signal_id']} {c['policy']} {c['symbol']}: {path} → {c['net_pnl']:+.2f}${r}, "
                    f"held {fmt_dur(c['exit_ms'] - c['entry_ms'])}")
            await eng.tg.send(text, dedupe_key=f"paper:{c['id']}", event="paper", signal_id=c["signal_id"],
                              reply_to=reply)

    # ---- reminders & summaries --------------------------------------------------
    def _london(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(ZoneInfo(self.cfg.app.display_tz))

    def bedtime_session(self, now: datetime) -> str | None:
        """Key (date of the evening) of the bedtime session that `now` falls in, else None."""
        bed = datetime.strptime(str(self.cfg.reminders.bedtime_reminder), "%H:%M").time()
        end = datetime.strptime(str(self.cfg.notifications.bedtime_end), "%H:%M").time()
        t = now.time()
        if t >= bed:
            return now.date().isoformat()
        if t < end:
            return (now.date() - timedelta(days=1)).isoformat()
        return None

    def snooze_key(self, now: datetime) -> str:
        """/snooze silences tonight: the current session, or the coming one if before bedtime."""
        return self.bedtime_session(now) or now.date().isoformat()

    async def reminders_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._bedtime()
                await self._daily_summary()
            except Exception:  # noqa: BLE001
                log.exception("reminder failed")

    async def _bedtime(self) -> None:
        eng = self.eng
        now = self._london()
        session = self.bedtime_session(now)
        if session is None or eng.db.get_state("bedtime_snoozed") == session:
            return
        views = eng.journal.list(include_closed=0)["open"]
        if not views:
            return
        repeat = self.cfg.reminders.bedtime_repeat_min * 60_000
        if eng.clock.now_ms() - self._last_bedtime_ms < repeat:
            return
        self._last_bedtime_ms = eng.clock.now_ms()
        lines = [f"🌙 Bedtime check - {len(views)} logged trade(s) still open:"]
        needs = []
        for v in views:
            p = v["precision"]
            pnl = f"{v['mtm_pnl']:+.2f}$" if v.get("mtm_pnl") is not None else "n/a"
            lines.append(f"• {v['symbol']} {v['signal_id'] or v['trade_ref']}: P&L ~{pnl} (unconfirmed), "
                         f"SL {fmt_price(v['current_stop'], p)}. Suggested: {v['next_action']}")
            if v["status"] == "NEEDS_CONFIRMATION":
                needs.append(f"• {v['symbol']} {v['trade_ref']}: {v.get('needs_confirmation') or 'check'}")
        if needs:
            lines += ["Unconfirmed (NEEDS_CONFIRMATION):", *needs]
        lines.append(f"Close on WEEX, then log it via Close all on the dashboard: "
                     f"{self.cfg.dashboard.base_url.rstrip('/')}/trades/close-all")
        lines.append("/snooze to silence this for tonight.")
        await eng.tg.send("\n".join(lines), dedupe_key=f"bedtime:{self._last_bedtime_ms}", event="bedtime")

    async def _daily_summary(self) -> None:
        eng = self.eng
        now = self._london()
        at = datetime.strptime(str(self.cfg.reminders.daily_summary), "%H:%M").time()
        today = now.date().isoformat()
        if now.time() < at or eng.db.get_state("daily_summary_date") == today:
            return
        eng.db.set_state("daily_summary_date", today)
        start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        d = stats_service.day_summary(eng.db, start, eng.clock.now_ms())

        def line(name, m):
            if not m.get("n"):
                return f"{name}: no closed trades"
            ar = f", avg {m['avg_r']:+.2f}R" if m.get("avg_r") is not None else ""
            return f"{name}: {m['n']} trades, win {m['win_rate'] * 100:.0f}%, net {m['net']:+.2f}${ar}"
        text = "\n".join([f"📊 Daily summary {today}", "System baseline (paper):", "  " + line("Policy L", d["L"]),
                          "  " + line("Policy S", d["S"]), "Your trades:", "  " + line("Closed", d["mine"]),
                          f"Open logged trades: {len(eng.journal.open_symbols())}"])
        await eng.tg.send(text, dedupe_key=f"daily:{today}", event="daily")

    # ---- backups -----------------------------------------------------------------
    def backup_now(self) -> Path | None:
        url = self.eng.db.url
        if not url.startswith("sqlite:///"):
            return None   # Postgres on the VPS: use pg_dump there
        src = Path(url[len("sqlite:///"):])
        bdir = self.cfg.data_dir / "backups"
        bdir.mkdir(exist_ok=True)
        dst = bdir / f"scanner-{self._london().date().isoformat()}.db"
        if dst.exists():
            return None
        with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
            a.backup(b)   # consistent copy while the app keeps writing
        keep = int(self.cfg.backup.keep)
        olds = sorted(bdir.glob("scanner-*.db"))
        for old in olds[:-keep]:
            old.unlink()   # only backup copies are rotated; the live DB is never touched
        log.info("DB backup written: %s", dst)
        return dst

    async def backup_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.backup_now)
            except Exception as e:  # noqa: BLE001
                self.eng.incident("backup_failed", str(e))
            await asyncio.sleep(3600)

    # ---- commands ------------------------------------------------------------------
    async def command(self, cmd: str) -> str | None:
        eng = self.eng
        if cmd == "/open":
            views = eng.journal.list(include_closed=0)["open"]
            if not views:
                return "No open logged trades."
            out = ["Open logged trades:"]
            for v in views:
                p = v["precision"]
                pnl = f"{v['mtm_pnl']:+.2f}$" if v.get("mtm_pnl") is not None else "n/a"
                flag = " ⚠️ NEEDS CONFIRMATION" if v["status"] == "NEEDS_CONFIRMATION" else ""
                out.append(f"• {v['symbol']} {v['signal_id'] or v['trade_ref']} entry {fmt_price(v['entry_price'], p)} "
                           f"P&L ~{pnl}, SL {fmt_price(v['current_stop'], p)}{flag}\n  next: {v['next_action']}")
            return "\n".join(out)
        if cmd == "/stats":
            s = stats_service.compute(eng.db)

            def fmt(m):
                if not m.get("n"):
                    return "no closed trades yet"
                pf = m.get("profit_factor")
                return (f"{m['n']} trades, win {m['win_rate'] * 100:.0f}%, exp {m['expectancy_usd']:+.2f}$"
                        + (f" / {m['expectancy_r']:+.2f}R" if m.get("expectancy_r") is not None else "")
                        + (f", PF {pf:.2f}" if isinstance(pf, (int, float)) else ""))
            return "\n".join([f"Signals recorded: {s['signals']}",
                              f"System L: {fmt(s['system']['overall']['L'])}",
                              f"System S: {fmt(s['system']['overall']['S'])}",
                              f"You: {fmt(s['mine']['overall'])}",
                              f"Full stats: {self.cfg.dashboard.base_url.rstrip('/')}/stats"])
        if cmd == "/mutepaper":
            muted = not eng.db.get_state("paper_muted", False)
            eng.db.set_state("paper_muted", muted)
            return "Paper-trade result lines muted. /mutepaper again to turn them back on." if muted \
                else "Paper-trade result lines on."
        if cmd == "/snooze":
            key = self.snooze_key(self._london())
            eng.db.set_state("bedtime_snoozed", key)
            return "Bedtime reminder snoozed for tonight."
        return None

    async def back_online_summary(self, lines: list[str]) -> None:
        eng = self.eng
        head = (f"🟢 Back online - {len(eng.universe.symbols)} symbols, regime "
                f"{eng.regime.state if eng.regime else 'n/a'}.")
        body = [head]
        if lines:
            body += ["Open logged trades (tracking resumed):", *[f"• {x}" for x in lines]]
        n_paper = len(eng.paper.open)
        if n_paper:
            body.append(f"Open paper trades: {n_paper}")
        await eng.tg.send("\n".join(body), dedupe_key=f"sys:online:{int(time.time() * 1000)}", event="system")
