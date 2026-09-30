"""Scanner engine: data layer, universe, features, regime (M1); levels, setups, scoring,
trade plans and Telegram signal alerts (M2); paper trading, journal tracking, reminders (M4,
see core/trading.py)."""
from __future__ import annotations

import asyncio
import logging
import time

from core import console
from core.clock import TF_MS, Clock, floor_tf, last_closed_open, next_bar_close
from core.config import load_env
from core.system_checks import sleep_warning
from data.bars import BarArrays, CandleStore
from data.db import Database
from data.funding import FundingBook
from data.market import PremiumTracker
from data.oi import OITracker
from data.orderflow import TradeTape
from data.universe import UniverseBuilder
from exchange.weex_rest import WeexRest
from exchange.weex_ws import PublicStream
from features.compute import Features, MarketInputs, compute_features
from journal.service import Journal
from notify import messages
from notify.telegram import Telegram
from plan.liquidation import Bracket, parse_risk_limits
from paper.book import PaperBook
from signals.engine import ScanRow, Signal, SignalEngine
from signals.regime import RISK_OFF, Regime, breadth_pct, compute_regime

log = logging.getLogger("engine")

TFS = ("5m", "15m", "1h", "4h")


def default_policy_missing(sig: dict, cfg) -> bool:
    """True when trade.require_default_policy is on and the default stop policy is n/a for this
    signal (e.g. Policy S when the structural stop doesn't fit) - your model would not trade it."""
    t = cfg.trade
    if not t.get("require_default_policy", False):
        return False
    key = {"S": "stop_s", "L": "stop_l"}.get(str(t.get("default_stop_policy", "L")).upper())
    plan = sig["plan"] if isinstance(sig, dict) else sig.plan
    sp = plan[key] if isinstance(plan, dict) else getattr(plan, key)
    price = sp["price"] if isinstance(sp, dict) else sp.price
    return key is not None and price is None
CRITICAL_KINDS = {"data_stale", "data_recovered", "symbol_delisted", "clock_drift", "taker_field_mismatch"}


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.clock = Clock(cfg.app.display_tz)
        url = cfg.database.url.format(data_dir=cfg.data_dir.as_posix())
        self.db = Database(url)
        self.rest = WeexRest(cfg, self.clock)
        self.rest.on_incident = self.incident
        self.store = CandleStore(cfg.bars.keep.to_dict())
        self.universe = UniverseBuilder(cfg, self.rest, self.db, self.clock)
        self.oi = OITracker(cfg, self.rest, self.db)
        self.premium = PremiumTracker(cfg, self.rest)
        self.tape = TradeTape(cfg)
        self.ws = PublicStream(cfg, self.tape.on_trade, self._ws_incident)
        self.btc = cfg.universe.regime_reference
        env = load_env()
        self.tg = Telegram(cfg, env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"], self.db)
        self.tg.on_command = self.handle_command
        self.highs = None
        self.recorder = None
        if cfg.get("research") and cfg.research.get("record_snapshots"):
            from research.recorder import Recorder
            self.recorder = Recorder(self)
        if cfg.get("highs") and cfg.highs.get("enabled"):
            from highs.service import HighsService
            self.highs = HighsService(self)
        self.signals = SignalEngine(cfg)
        self.brackets: dict[str, list[Bracket]] = {}
        self.features: dict[str, Features] = {}
        self.scan_rows: dict[str, ScanRow] = {}
        self.recent_signals: list[Signal] = []
        self.regime: Regime | None = None
        self.healthy = True
        self.last_bar_ms = 0          # close time of the latest 5m bar processed
        self.last_scan_ms = 0
        self._tasks: list[asyncio.Task] = []
        self._taker_mismatch_alerted = False
        self.hub = None               # web.server.WebHub once the dashboard runs
        self._feat_memo: dict = {}    # 15m/1h indicator cache (see features.compute)
        self.starting = True          # until the first universe/history load completes
        self.funding = FundingBook(self.rest)
        self.paper = PaperBook(cfg, self.db, self.funding)
        self.journal = Journal(self)
        from core.trading import Trading
        self.trading = Trading(self)

    # ---- helpers ---------------------------------------------------------

    def incident(self, kind: str, message: str, symbol: str | None = None, severity: str = "warning") -> None:
        try:
            self.db.incident(kind, message, symbol, severity)
        except Exception:  # noqa: BLE001 - never let logging an incident crash the engine
            log.exception("failed to record incident")
        if kind in CRITICAL_KINDS:
            if kind == "taker_field_mismatch":
                if self._taker_mismatch_alerted:
                    return
                self._taker_mismatch_alerted = True
            icon = "✅" if kind == "data_recovered" else "⚠️"
            text = f"{icon} {kind.replace('_', ' ').upper()}{' ' + symbol if symbol else ''}: {message}"
            self._spawn(self.tg.send(text, dedupe_key=f"sys:{kind}:{symbol}:{int(time.time() * 1000)}",
                                     event="system"))

    def _spawn(self, coro) -> None:
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()

    def _ws_incident(self, kind: str, message: str) -> None:
        if kind == "ws_disconnect":
            self.tape.reset_observation()
        self.incident(kind, message)

    @property
    def tracked(self) -> list[str]:
        """Symbols we keep bars + trades for: references, the universe, and any symbol with an
        open paper or logged trade (even if it left the universe)."""
        refs = list(dict.fromkeys([self.btc, *self.cfg.universe.majors]))
        return list(dict.fromkeys(refs + self.universe.symbols + sorted(self.trading.symbols())))

    async def ensure_tracked(self, sym: str) -> None:
        """A trade was logged on a symbol we don't follow yet: load bars and subscribe now."""
        if sym not in self.store.symbols():
            await self.warmup([sym])
        if self.ws.conns:
            await self.ws.set_symbols(self.tracked)

    async def replay_bars(self, sym: str, since_ms: int) -> tuple[BarArrays, BarArrays, BarArrays | None]:
        """5m + 15m bars (and 5m mark bars when stops trigger on mark) covering since_ms..now,
        for late-logging replay and restart catch-up."""
        b5 = self.store.series(sym, "5m").arrays() if self.store.has(sym) else None
        b15 = self.store.series(sym, "15m").arrays() if self.store.has(sym) else None
        if b5 is None or not len(b5) or b5.t[0] > since_ms:
            b5 = BarArrays.from_bars(await self.rest.klines(sym, "5m", 1000))
            if not len(b5) or b5.t[0] > since_ms:
                b5 = BarArrays.from_bars(await self.rest.history_klines(sym, "5m", since_ms, self.clock.now_ms()))
        if b15 is None or not len(b15) or b15.t[0] > since_ms - 60 * TF_MS["15m"]:
            b15 = BarArrays.from_bars(await self.rest.klines(sym, "15m", 1000))
        mark5 = None
        if self.cfg.trade.stop_trigger_price == "mark":
            try:
                mark5 = BarArrays.from_bars(await self.rest.mark_klines(sym, "5m", 1000))
            except Exception as e:  # noqa: BLE001
                log.warning("mark klines %s failed (%s): replay uses last prices", sym, e)
        return b5, b15, mark5

    def precision(self, sym: str) -> int | None:
        m = self.universe.meta.get(sym)
        return int(m["pricePrecision"]) if m and m.get("pricePrecision") is not None else None

    @property
    def paused(self) -> bool:
        return bool(self.db.get_state("signals_paused", False))

    @property
    def watch_muted(self) -> bool:
        return bool(self.db.get_state("watch_muted", False))

    async def check_clock(self) -> None:
        server, before, after = await self.rest.server_time()
        drift = self.clock.set_offset(server, before, after)
        msg = f"local clock differs from WEEX server time by {drift:+.3f}s (corrected internally)"
        if abs(drift) > self.cfg.exchange.clock_drift_warn_s:
            self.incident("clock_drift", msg)
            print(f"WARNING: {msg}. Consider syncing Windows time (Settings > Time > Sync now).")
        else:
            log.info(msg)

    async def refresh_risk_limits(self) -> None:
        try:
            self.brackets = parse_risk_limits(await self.rest.risk_limits_all())
        except Exception as e:  # noqa: BLE001
            self.incident("risk_limits_failed", str(e))

    # ---- bars ------------------------------------------------------------

    async def _warm_symbol(self, sym: str) -> None:
        for tf in TFS:
            try:
                bars = await self.rest.klines(sym, tf, self.cfg.bars.warmup_limit[tf])
                self.store.series(sym, tf).merge(bars)
            except Exception as e:  # noqa: BLE001
                log.warning("warmup %s %s failed: %s", sym, tf, e)

    async def warmup(self, symbols: list[str]) -> None:
        new = [s for s in symbols if not self.store.has(s)]
        if new:
            t0 = time.monotonic()
            await asyncio.gather(*(self._warm_symbol(s) for s in new))
            log.info("warmed %d symbols in %.1fs", len(new), time.monotonic() - t0)

    async def _refresh_one(self, sym: str, tf: str, expected_open: int) -> bool:
        """Fetch recently closed bars; back-fills gaps. True if the expected bar is present."""
        series = self.store.series(sym, tf)
        last = series.last
        step = TF_MS[tf]
        limit = self.cfg.bars.refresh_limit
        if last is None:
            limit = self.cfg.bars.warmup_limit[tf]
        elif expected_open - last.t > step:
            limit = min(1000, (expected_open - last.t) // step + limit)
        try:
            series.merge(await self.rest.klines(sym, tf, limit))
        except Exception as e:  # noqa: BLE001
            log.warning("refresh %s %s failed: %s", sym, tf, e)
        last = series.last
        return last is not None and last.t >= expected_open

    async def refresh_closed(self, tf: str, symbols: list[str]) -> list[str]:
        """Bring every symbol up to the latest closed bar of tf. Returns symbols still missing it."""
        expected = last_closed_open(self.clock.now_ms(), tf)
        pending = [s for s in symbols
                   if (last := self.store.series(s, tf).last) is None or last.t < expected]
        for _attempt in range(self.cfg.exchange.bar_fetch_retries):
            ok = await asyncio.gather(*(self._refresh_one(s, tf, expected) for s in pending))
            pending = [s for s, good in zip(pending, ok) if not good]
            if not pending:
                break
            await asyncio.sleep(self.cfg.exchange.bar_fetch_retry_s)
        if pending:
            log.warning("%s bar %s not available for %d symbols: %s", tf, self.clock.fmt(expected),
                        len(pending), ", ".join(pending[:8]))
        return pending

    # ---- features & regime ----------------------------------------------

    def _market_inputs(self, sym: str, at_ms: int) -> MarketInputs:
        p = self.premium.data.get(sym)
        return MarketInputs(
            oi_chg_15m=self.oi.change(sym, 15, at_ms),
            oi_chg_1h=self.oi.change(sym, 60, at_ms),
            oi_chg_4h=self.oi.change(sym, 240, at_ms),
            funding_8h=self.premium.funding_8h(sym),
            next_funding_ms=p.next_funding_ms if p else None,
        )

    def compute_all(self, as_of: int) -> None:
        """Features for every universe symbol + regime, using bars closed by as_of only."""
        btc5 = self.store.series(self.btc, "5m").arrays().upto(as_of)
        feats: dict[str, Features] = {}
        for sym in self.universe.symbols:
            b5 = self.store.series(sym, "5m").arrays().upto(as_of)
            b15 = self.store.series(sym, "15m").arrays().upto(as_of)
            b1h = self.store.series(sym, "1h").arrays().upto(as_of)
            if len(b5) < 50 or len(b15) < 5 or len(b1h) < 5:
                continue
            try:
                feats[sym] = compute_features(sym, b5, b15, b1h, btc5, self._market_inputs(sym, as_of), self.cfg,
                                              memo=self._feat_memo)
            except Exception:  # noqa: BLE001
                log.exception("features failed for %s", sym)
        self.features = feats

        uni_1h = [self.store.series(s, "1h").arrays().upto(as_of) for s in self.universe.symbols]
        br, n = breadth_pct(uni_1h, self.cfg.regime.breadth_ema_1h)
        btc15 = self.store.series(self.btc, "15m").arrays().upto(as_of)
        btc1h = self.store.series(self.btc, "1h").arrays().upto(as_of)
        if len(btc5) and len(btc15) and len(btc1h):
            self.regime = compute_regime(btc5, btc15, btc1h, br, n, self.cfg)

    # ---- signals & alerts -----------------------------------------------

    def _ref_price(self, sym: str, close: float) -> float:
        px = self.tape.last_price.get(sym)
        t = self.tape.last_trade_ms.get(sym, 0)
        if px and self.clock.now_ms() - t <= self.cfg.signals.last_price_max_age_s * 1000:
            return px
        return close

    def scan(self, as_of: int, live_prices: bool = True, dry_run: bool = False) -> tuple[list[Signal], list]:
        """Evaluate setups for all symbols on the bar closed at as_of."""
        rows: dict[str, ScanRow] = {}
        new_signals: list[Signal] = []
        new_watches = []
        for sym, f in self.features.items():
            arr = {tf: self.store.series(sym, tf).arrays().upto(as_of) for tf in TFS}
            ref = self._ref_price(sym, f.price) if live_prices else f.price
            try:
                row, sigs, watch = self.signals.evaluate(sym, as_of, f, arr["5m"], arr["15m"], arr["1h"], arr["4h"],
                                                         self.regime, self.brackets.get(sym), ref,
                                                         self.precision(sym), dry_run=dry_run)
            except Exception:  # noqa: BLE001
                log.exception("signal evaluation failed for %s", sym)
                continue
            rows[sym] = row
            new_signals += sigs
            if watch:
                new_watches.append((watch, f))
        self.scan_rows = rows
        self.last_scan_ms = self.clock.now_ms()
        return new_signals, new_watches

    def _suppression(self, sig: Signal, sent_last_hour: int) -> str | None:
        if self.paused:
            return "paused"
        if self.cfg.signals.get("suppress_longs_in_risk_off", True) and (
                "RISK_OFF" in sig.tags or (self.regime and self.regime.state == RISK_OFF)):
            return "risk_off"
        if any(t.startswith("BLACKOUT") for t in sig.tags) and not self.cfg.signals.blackout_alerts:
            return "blackout"
        if default_policy_missing(sig, self.cfg):
            return "default_policy_na"
        if sent_last_hour >= self.cfg.signals.max_alerts_per_hour:
            return "hourly_cap"
        return None

    async def publish_signals(self, sigs: list[Signal]) -> None:
        """Persist every signal; alert in score order unless suppressed."""
        now = self.clock.now_ms()
        sent_last_hour = self.db.count_alerts_since("entry", now - 3_600_000)
        for sig in sorted(sigs, key=lambda s: s.score, reverse=True):
            rec = sig.to_record()
            sid = self.db.insert_signal(symbol=sig.symbol, setup=sig.setup, bar_close_ms=sig.bar_close_ms,
                                        score=sig.score, regime=sig.regime.get("state", "?"), session=sig.session,
                                        config_hash=self.cfg.hash, data=rec)
            if sid is None:
                continue  # already recorded (restart on the same bar) - never duplicate
            sig.signal_id = rec["signal_id"] = sid
            reason = self._suppression(sig, sent_last_hour)
            sig.suppressed_reason = rec["suppressed_reason"] = reason
            try:
                self.paper.open_for_signal(rec)          # baseline: every signal, sent or not
            except Exception:  # noqa: BLE001
                log.exception("paper open failed for %s", sid)
            self.recent_signals = (self.recent_signals + [sig])[-50:]
            if reason:
                self.db.update_signal_alert(sid, "suppressed", reason)
                print(f"SIGNAL {sid} {sig.setup} {sig.symbol} score {sig.score} - suppressed ({reason})")
                continue
            text = messages.entry_message(rec, now, self.cfg, self.precision(sig.symbol),
                                          already_holding=self.journal.holding(sig.symbol))
            msg_id = await self.tg.send(text, dedupe_key=f"{sid}:entry", event="entry", signal_id=sid)
            status = "console" if not self.tg.enabled else "sent" if msg_id else "failed"
            self.db.update_signal_alert(sid, status, None, msg_id)
            sent_last_hour += 1
            print(f"SIGNAL {sid} {sig.setup} {sig.symbol} score {sig.score} - alerted")

    async def publish_watches(self, watches: list) -> None:
        if not self.cfg.signals.watch_alerts or self.watch_muted or self.paused:
            return
        for w, f in watches:
            text = messages.watch_message(w.symbol, w.box_high, w.box_low, f, self.precision(w.symbol), self.cfg)
            await self.tg.send(text, dedupe_key=f"watch:{w.symbol}:{w.since_ms}", event="watch")

    def restore_state(self) -> None:
        """After a restart: re-apply per-symbol cooldowns from signals already recorded."""
        since = self.clock.now_ms() - self.cfg.signals.symbol_cooldown_min * 60_000
        for row in self.db.recent_signals(since):
            prev = self.signals.last_signal_ms.get(row.symbol, 0)
            self.signals.last_signal_ms[row.symbol] = max(prev, row.bar_close_ms)

    # ---- telegram commands ----------------------------------------------

    async def handle_command(self, cmd: str, arg: str) -> str:
        if cmd in ("/start", "/help"):
            return "Commands: /status /top /open /stats /pause /resume /mute /unmute /mutepaper /snooze"
        if cmd == "/status":
            r = self.regime
            reg = (f"{r.state}{' BTC_DUMP' if r.btc_dump else ''} | BTC {r.btc_price:,.1f} ({r.btc_ret_1h:+.2f}% 1h) "
                   f"| breadth {r.breadth:.0f}%") if r else "regime n/a"
            watches = [s for s, row in self.scan_rows.items() if row.state == "WATCH"]
            return (f"{reg}\nUniverse {len(self.universe.symbols)} | data {'OK' if self.healthy else 'STALE'} | "
                    f"WS {'OK' if self.ws.connected else 'DOWN'}\n"
                    f"Signals {'PAUSED' if self.paused else 'on'} | WATCH alerts {'muted' if self.watch_muted else 'on'}\n"
                    f"Last scan {self.clock.fmt(self.last_scan_ms)} London | watching: {', '.join(watches) or '-'}")
        if cmd == "/top":
            rows = sorted(self.scan_rows.values(), key=lambda r: r.score, reverse=True)[:10]
            if not rows:
                return "No scan yet."
            return "Top by score:\n" + "\n".join(
                f"{r.symbol} {r.state} {r.score} ({r.setup.lower()} {r.n_pass}/{r.n_conds}) room {r.headroom_pct:.1f}%"
                for r in rows)
        if cmd == "/pause":
            self.db.set_state("signals_paused", True)
            return "Signals paused: ENTRY alerts are logged but not sent. /resume to turn back on."
        if cmd == "/resume":
            self.db.set_state("signals_paused", False)
            return "Signals resumed."
        if cmd == "/mute":
            self.db.set_state("watch_muted", True)
            return "WATCH alerts muted (ENTRY alerts still come). /unmute to undo."
        if cmd == "/unmute":
            self.db.set_state("watch_muted", False)
            return "WATCH alerts on."
        if cmd in ("/open", "/stats", "/mutepaper", "/snooze"):
            return await self.trading.command(cmd)
        return "Unknown command. /help"

    # ---- console ---------------------------------------------------------

    def print_scan(self) -> None:
        ws = self.ws.connected if self.ws.conns else None
        print(console.header(self.clock, self.regime, len(self.universe.symbols), ws,
                             self.rest.limiter.used(), self.healthy,
                             self.oi.history_minutes(self.btc)))
        print(console.table(list(self.features.values()), self.scan_rows, self.cfg.console.top_n), flush=True)

    def trade_checks(self) -> None:
        if not self.cfg.orderflow.trade_check:
            return
        for sym in self.tracked:
            bar = self.store.series(sym, "5m").last
            if bar is None:
                continue
            problem = self.tape.check_bar(sym, bar)
            if problem:
                self.incident("taker_field_mismatch", problem, sym)
        log.info("trade-tape check: %d bars agree, %d disagree (cumulative)",
                 self.tape.checks_ok, self.tape.checks_bad)

    # ---- loops -----------------------------------------------------------

    async def _sleep_until(self, ms: int) -> None:
        await asyncio.sleep(max(0.0, (ms - self.clock.now_ms()) / 1000))

    async def on_bar_close(self, as_of: int, live_prices: bool = True) -> None:
        self.compute_all(as_of)
        sigs, watches = self.scan(as_of, live_prices)
        if not self.healthy:
            if sigs:
                log.warning("data stale: %d signal(s) discarded", len(sigs))
            return  # stale data: signals paused until healthy again
        await self.publish_signals(sigs)
        await self.publish_watches(watches)
        if self.recorder:
            try:
                books = await self.recorder.books(list(self.features))
                rows = self.recorder.rows(as_of, sigs, books)
                await asyncio.to_thread(self.recorder.write, as_of, rows)
            except Exception:  # noqa: BLE001 - research data never disturbs the scanner
                log.exception("research recorder failed")

    async def bar_loop(self) -> None:
        delay = int(self.cfg.exchange.bar_close_delay_s * 1000)
        while True:
            nxt = next_bar_close(self.last_bar_ms, self.clock.now_ms())
            await self._sleep_until(nxt + delay)
            syms = self.tracked
            for tf in TFS:  # higher timeframes only fetch when a newer closed bar is due
                await self.refresh_closed(tf, syms)
            self.last_bar_ms = nxt
            if nxt % TF_MS["15m"] == 0:
                await self.trading.on_15m(nxt)
            self.trade_checks()
            try:
                await self.on_bar_close(nxt)
            except Exception:  # noqa: BLE001
                log.exception("bar processing failed")
            self.print_scan()
            await self.push_state()

    async def push_state(self) -> None:
        if self.hub is None:
            return
        from web import views
        try:
            await self.hub.broadcast(views.state(self))
        except Exception:  # noqa: BLE001
            log.exception("dashboard push failed")

    async def universe_loop(self) -> None:
        period = self.cfg.universe.refresh_min * 60
        while True:
            await asyncio.sleep(period)
            try:
                before = set(self.universe.symbols)
                await self.universe.refresh()
                await self.refresh_risk_limits()
                self._handle_delistings()
                await self.warmup(self.tracked)
                await self.ws.set_symbols(self.tracked)
                added, removed = set(self.universe.symbols) - before, before - set(self.universe.symbols)
                if added or removed:
                    print(f"universe update: +{sorted(added)} -{sorted(removed)}")
            except Exception as e:  # noqa: BLE001
                self.incident("universe_refresh_failed", str(e))

    def _handle_delistings(self) -> None:
        listed = self.universe.exchange_symbols
        for sym in self.store.symbols():
            if listed and sym not in listed:
                held = sym in self.journal.open_symbols()
                msg = f"{sym} no longer listed on WEEX; dropped"
                if held:
                    msg += ". YOU HAVE AN OPEN LOGGED TRADE IN IT - check WEEX now"
                self.incident("symbol_delisted", msg, sym, "critical")
                self.store.drop(sym)

    async def oi_loop(self) -> None:
        period = self.cfg.open_interest.poll_s
        while True:
            t0 = time.monotonic()
            try:
                await self.oi.poll(self.tracked)
            except Exception as e:  # noqa: BLE001
                log.warning("OI poll failed: %s", e)
            await asyncio.sleep(max(1.0, period - (time.monotonic() - t0)))

    async def premium_loop(self) -> None:
        while True:
            try:
                await self.premium.poll()
                self.funding.observe(self.premium.data)
            except Exception as e:  # noqa: BLE001
                log.warning("premiumIndex poll failed: %s", e)
            await asyncio.sleep(self.cfg.funding.poll_s)

    async def clock_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.exchange.clock_check_interval_min * 60)
            try:
                await self.check_clock()
            except Exception as e:  # noqa: BLE001
                log.warning("clock check failed: %s", e)

    async def watchdog_loop(self) -> None:
        stale_s = self.cfg.watchdog.stale_after_s
        while True:
            await asyncio.sleep(15)
            now = self.clock.now_ms()
            problems = []
            ws_age = time.monotonic() - self.ws.last_msg if self.ws.last_msg else None
            if ws_age is None or ws_age > stale_s:
                problems.append(f"no WS trades for {ws_age or 0:.0f}s")
            expected_close = floor_tf(now, "5m")
            if now - expected_close > (self.cfg.exchange.bar_close_delay_s + stale_s) * 1000 \
                    and self.last_bar_ms < expected_close:
                problems.append("5m bars not updating")
            if now - self.premium.last_poll_ms > stale_s * 1000:
                problems.append("mark/funding stale")
            if problems and self.healthy:
                self.healthy = False
                self.incident("data_stale", "; ".join(problems) + " - signals paused", severity="critical")
                print("DATA STALE: " + "; ".join(problems))
            elif not problems and not self.healthy:
                self.healthy = True
                self.incident("data_recovered", "data healthy again - signals resumed", severity="info")
                print("Data healthy again.")

    # ---- lifecycle -------------------------------------------------------

    async def startup(self) -> None:
        warn = sleep_warning()
        if warn:
            print("WARNING: " + warn)
            self.incident("power_plan", warn)
        if not self.tg.enabled:
            print("Telegram not configured (.env) - alerts will be printed here instead.")
        self.db.record_config(self.cfg.hash, self.cfg.to_dict(), "startup")
        await self.check_clock()
        print("Building universe (order books, listing age, ATR, wick risk)...", flush=True)
        await self.universe.refresh()
        await self.refresh_risk_limits()
        print(f"Universe: {len(self.universe.symbols)} symbols. Loading bar history...", flush=True)
        await self.warmup(self.tracked)
        self.oi.load_history(self.clock.now_ms())
        self.restore_state()
        self.paper.load_open()
        await self.warmup(self.tracked)   # symbols with open trades outside the universe
        await self.premium.poll()
        await self.oi.poll(self.tracked)
        as_of = floor_tf(self.clock.now_ms(), "5m")
        await self.refresh_closed("5m", self.tracked)
        self.last_bar_ms = as_of
        self.compute_all(as_of)
        # Evaluate the current bar for display and to rebuild WATCH state, but do not alert on it:
        # it may have closed minutes ago and its alert (if any) was handled before a restart.
        # dry_run: this display-only pass must not start cooldowns or consume WATCHes.
        self.scan(as_of, live_prices=False, dry_run=True)

    def print_universe(self) -> None:
        kept = set(self.universe.symbols)
        rej = [c for c in self.universe.candidates if c.reasons]
        print(f"\nUniverse ({len(kept)}): " + ", ".join(self.universe.symbols))
        print(f"Rejected after volume filter ({len(rej)}): " +
              "; ".join(f"{c.symbol} [{', '.join(c.reasons)}]" for c in rej[:40]) +
              (" ..." if len(rej) > 40 else ""))

    async def run(self, once: bool = False) -> None:
        try:
            if not once:  # dashboard first, so it shows "starting" while history loads
                from web.server import WebHub, price_loop, serve
                self.hub = WebHub()
                self._tasks += [asyncio.create_task(serve(self, self.hub), name="dashboard"),
                                asyncio.create_task(price_loop(self, self.hub), name="price_push")]
            await self.startup()
            self.starting = False
            self.print_universe()
            self.print_scan()
            if once:
                return
            await self.push_state()
            await self.trading.catch_up_paper()
            lines = await self.journal.load_and_catch_up()
            await self.trading.back_online_summary(lines)
            await self.ws.set_symbols(self.tracked)
            if self.recorder:
                from research.recorder import nightly_loop
                self._tasks.append(asyncio.create_task(nightly_loop(self), name="research_nightly"))
            if self.highs:
                await asyncio.to_thread(self.highs.load)
                self._tasks += [asyncio.create_task(self.highs.scan_loop(), name="highs_scan"),
                                asyncio.create_task(self.highs.history_loop(), name="highs_history")]
            self._tasks += [asyncio.create_task(c(), name=c.__name__) for c in (
                self.bar_loop, self.universe_loop, self.oi_loop, self.premium_loop,
                self.clock_loop, self.watchdog_loop, self.tg.poll_commands, self.trading.track_loop,
                self.trading.reminders_loop, self.trading.backup_loop)]
            await asyncio.gather(*self._tasks)
        finally:
            for t in self._tasks:
                t.cancel()
            await self.ws.stop()
            await self.rest.close()
            if self.highs:
                await self.highs.cg.close()
            await self.tg.close()
