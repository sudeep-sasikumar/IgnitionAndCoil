"""Highs tab service: scans the CoinGecko top N every few minutes, detects 52-week-high and
all-time-high breaks (highs.detector), records them, alerts on Telegram and feeds the dashboard.

Every break also carries its PEAK: the highest price since the break (how far it ran). It is
updated at every scan for highs.peak_track_days from the price, CoinGecko's 24h high and its ATH.
Breaks we did not watch from the start (recorded before this existed, or the scanner was down
for most of a day) are filled once from CoinGecko candles by peak_loop.

APPROACHING: every scan also lists the coins within highs.approach_pct below the level whose
break would be reported next (highs.detector.next_level), and alerts once per coin and level,
so a buy-stop order can be placed AT the level before the break (the breakout study in
docs/DECISIONS.md found the first minutes after the cross matter most). Signals only.

Three loops share one paced CoinGecko client:
- peak_loop: one request per coin, only when a break's peak has to be filled from candles
- scan_loop: /coins/markets for the top N (8 requests for 2,000 coins) every highs.scan_min
- history_loop: a year of OHLC candles per coin (one request each, best-ranked first) - needed
  for the 52-week high. With the keyless API that takes hours for 2,000 coins; ATH breaks are
  detected from the first scan on (CoinGecko reports each coin's ATH).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from core.config import load_env
from highs import store
from highs.coingecko import CoinGecko, parse_iso_ms
from highs.detector import (ATH, DAY, CoinState, Obs, check, excluded, merge_candles, next_level, ohlc_days,
                            peak_after, update)

log = logging.getLogger("highs")
STALE_HIST = 5 * DAY          # reload candles after a gap in our own daily observations (candles are 4-day)
WATCH_GAP = 23 * 3_600_000    # a longer gap between two scans of a coin is not covered by its 24h high
APPROACH_STATE = "highs_approach_sent"      # app_state key: {cg_id: [level, alerted_ms]}


def fmt_usd(x: float | None) -> str:
    if x is None:
        return "–"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"${x / div:.1f}{suf}"
    return f"${x:,.0f}"


def fmt_price(x: float | None) -> str:
    if x is None:
        return "–"
    if x >= 1000:
        return f"${x:,.1f}"
    if x >= 1:
        return f"${x:,.3f}"
    return f"${x:.4g}" if x >= 0.0001 else f"${x:.3e}"


def age_text(ms: int | None, now_ms: int) -> str:
    if not ms:
        return "?"
    d = (now_ms - ms) / DAY
    return f"{d / 365:.1f}y ago" if d >= 365 else f"{d:.0f}d ago"


class HighsService:
    def __init__(self, eng):
        self.eng, self.cfg, self.db = eng, eng.cfg, eng.db
        h = self.cfg.highs
        self.h = h
        key = load_env().get("COINGECKO_DEMO_API_KEY", "")
        self.cg = CoinGecko(h.api_base, h.calls_per_min, key)
        self.keyed = bool(key)
        self.states: dict[str, CoinState] = {}
        self.meta: dict[str, dict] = {}         # cg_id -> latest market row fields (rank, price, ...)
        self.top_ids: list[str] = []
        self.last_scan_ms = 0
        self.last_error: str | None = None
        self.stables = {s.upper() for s in self.cfg.universe.stablecoin_bases}
        self.track_ms = int(float(h.get("peak_track_days", 30)) * DAY)
        self.tracked: dict[str, list[dict]] = {}   # cg_id -> its breaks whose peak is still followed
        self.refill: dict[str, None] = {}          # coins whose peaks need candles (ordered set)
        self.approaching: list[dict] = []          # coins just under a level (rebuilt every scan)
        self.approach_sent: dict[str, list] = {}   # cg_id -> [level, when alerted]
        plan = h.get("approach_plan") or {}
        self.plan = (float(plan.get("target_pct", 10)), float(plan.get("stop_pct", 4)))

    def load(self) -> None:
        self.approach_sent = dict(self.db.get_state(APPROACH_STATE, {}) or {})
        for cg_id, (st, row) in store.load_states(self.db).items():
            self.states[cg_id] = st
            self.meta[cg_id] = {"rank": row.rank, "price": row.price, "market_cap": row.market_cap,
                                "volume": row.volume, "updated_ms": row.updated_ms}
        for e in store.peak_events(self.db, self.eng.clock.now_ms() - self.track_ms):
            self.tracked.setdefault(e.cg_id, []).append(
                {"id": e.id, "ts": e.ts, "base": max(e.level, e.price), "peak": e.peak, "peak_ms": e.peak_ms})
            if e.peak is None:
                self.refill[e.cg_id] = None
        log.info("highs: %d coins loaded from the database, %d coins with breaks to fill a peak for",
                 len(self.states), len(self.refill))

    # ---- peak since the break -----------------------------------------------------------------
    def track_peaks(self, cg_id: str, ob: Obs, now: int, prev_seen_ms: int) -> dict[int, tuple[float, int]]:
        """New peaks for this coin's followed breaks, from one observation."""
        out: dict[int, tuple[float, int]] = {}
        evs = self.tracked.get(cg_id)
        if not evs:
            return out
        if now - prev_seen_ms > WATCH_GAP:
            self.refill[cg_id] = None
        hi = max(ob.price, ob.high_24h or 0.0)   # the 24h high may reach back before the break, where
        for t in evs:                            #   price was below the old high: it cannot overstate
            if t["peak"] is None or now > t["ts"] + self.track_ms:
                continue                         # not filled yet (peak_loop) / no longer followed
            best = (t["peak"], t["peak_ms"])
            if hi > best[0]:
                best = (hi, now)
            if ob.ath is not None and ob.ath_ms and ob.ath_ms >= t["ts"] and ob.ath >= best[0]:
                best = (float(ob.ath), ob.ath_ms)        # CoinGecko's exact record and its time
            if best != (t["peak"], t["peak_ms"]):
                t["peak"], t["peak_ms"] = best
                out[t["id"]] = best
        return out

    def fill_peaks(self, cg_id: str, candles: list[list[float]]) -> dict[int, tuple[float, int]]:
        """Peaks for this coin's breaks from OHLC candles (never lowers a peak already seen)."""
        out: dict[int, tuple[float, int]] = {}
        for t in self.tracked.get(cg_id, []):
            best = (t["base"], t["ts"]) if t["peak"] is None else (t["peak"], t["peak_ms"])
            c = peak_after(candles, t["ts"], t["ts"] + self.track_ms)
            if c and c[0] > best[0]:
                best = c
            if best != (t["peak"], t["peak_ms"]):
                t["peak"], t["peak_ms"] = best
                out[t["id"]] = best
        return out

    async def peak_loop(self) -> None:
        while True:
            if not self.refill:
                await asyncio.sleep(60)
                continue
            cg_id = next(iter(self.refill))
            evs = self.tracked.get(cg_id)
            if not evs:
                self.refill.pop(cg_id, None)
                continue
            try:
                candles = await self.cg.ohlc(cg_id, ohlc_days(self.eng.clock.now_ms() - min(t["ts"] for t in evs)))
            except Exception as e:  # noqa: BLE001 - try the other coins first, this one again later
                log.warning("highs: peak candles for %s failed: %s", cg_id, e)
                self.refill.pop(cg_id, None)
                self.refill[cg_id] = None
                await asyncio.sleep(60)
                continue
            self.refill.pop(cg_id, None)
            peaks = self.fill_peaks(cg_id, candles if isinstance(candles, list) else [])
            await asyncio.to_thread(store.set_peaks, self.db, peaks)

    def weex_symbol(self, symbol: str, price: float | None = None) -> str | None:
        """<COIN>USDT if WEEX lists it AND it is the same asset: tickers are not unique, so a perp
        whose price is further than highs.weex_match_max_diff_pct from the coin's is another coin."""
        s = f"{symbol.upper()}USDT"
        if s not in self.eng.universe.exchange_symbols:
            return None
        mark = self.eng.premium.mark(s)
        if price and mark and abs(mark / price - 1) * 100 > float(self.h.get("weex_match_max_diff_pct", 5)):
            return None
        return s

    # ---- approaching a level ------------------------------------------------------------------
    def approach_row(self, st: CoinState, price: float, m: dict, now: int) -> dict | None:
        """The coin's row for the Approaching list, or None if it is not just under a level."""
        h = self.h
        nl = next_level(st, now, h.min_high_age_days, h.min_history_days)
        if nl is None or price >= nl.level:
            return None
        dist = (nl.level / price - 1) * 100
        if dist > float(h.get("approach_pct", 3)):
            return None
        tp, sl = self.plan
        return {"cg_id": st.cg_id, "symbol": st.symbol.upper(), "name": st.name, "rank": m["rank"], "kind": nl.kind,
                "level": nl.level, "level_ms": nl.prev_high_ms, "price": price, "dist_pct": dist,
                "target": nl.level * (1 + tp / 100), "stop": nl.level * (1 - sl / 100),
                "market_cap": m["market_cap"], "volume": m["volume"], "weex_symbol": self.weex_symbol(st.symbol, price)}

    def approach_new(self, now: int) -> list[dict]:
        """Rows to alert now: not alerted for this coin and level within highs.approach_realert_h."""
        h = self.h
        again = float(h.get("approach_realert_h", 24)) * 3_600_000
        out = []
        for a in self.approaching:
            if h.get("approach_weex_only", True) and not a["weex_symbol"]:
                continue
            if (a["volume"] or 0) < float(h.alert_min_volume_usd):
                continue
            prev = self.approach_sent.get(a["cg_id"])
            if prev and abs(prev[0] / a["level"] - 1) < 1e-9 and now - prev[1] < again:
                continue
            out.append(a)
        return out

    async def alert_approaching(self, now: int) -> None:
        h = self.h
        new = self.approach_new(now)
        if not new or not h.get("approach_telegram", True):
            return
        n = int(h.max_lines_per_alert)
        for i in range(0, len(new), n):
            chunk = new[i:i + n]
            text = approach_text(chunk, now, self.plan, self.cfg.dashboard.base_url, len(new) > n)
            await self.eng.tg.send(text, dedupe_key=f"highs-approach:{now}:{i}", event="highs_approach")
            for a in chunk:                                  # once per level, sent or printed
                self.approach_sent[a["cg_id"]] = [a["level"], now]
        self.approach_sent = {k: v for k, v in self.approach_sent.items() if now - v[1] < 7 * DAY}
        await asyncio.to_thread(self.db.set_state, APPROACH_STATE, self.approach_sent)

    # ---- scanning ---------------------------------------------------------------------------
    async def scan(self) -> list[dict]:
        h = self.h
        now = self.eng.clock.now_ms()
        rows = await self.cg.top(int(h.top_n))
        events: list[dict] = []
        coin_rows: list[dict] = []
        peaks: dict[int, tuple[float, int]] = {}
        near: list[dict] = []
        top: list[str] = []
        for r in rows:
            cg_id, price = r.get("id"), r.get("current_price")
            if not cg_id or not isinstance(price, (int, float)) or price <= 0:
                continue
            sym, name = str(r.get("symbol") or ""), str(r.get("name") or "")
            if excluded(name, sym, self.stables, list(h.exclude_name_keywords)):
                continue
            top.append(cg_id)
            st = self.states.get(cg_id) or CoinState(cg_id, sym, name)
            first_seen = cg_id not in self.states
            ob = Obs(float(price), r.get("high_24h"), r.get("ath"), parse_iso_ms(r.get("ath_date")))
            breaks = [] if first_seen else check(st, ob, now, h.min_high_age_days, h.min_history_days)
            peaks.update(self.track_peaks(cg_id, ob, now, self.meta.get(cg_id, {}).get("updated_ms") or 0))
            update(st, ob, now)
            st.symbol, st.name = sym, name
            self.states[cg_id] = st
            m = {"rank": r.get("market_cap_rank"), "price": float(price), "market_cap": r.get("market_cap"),
                 "volume": r.get("total_volume"), "updated_ms": now}
            self.meta[cg_id] = m
            coin_rows.append({"cg_id": cg_id, "symbol": sym, "name": name, **m, "ath": st.ath, "ath_ms": st.ath_ms,
                              "hist": st.hist, "hist_ok": st.hist_ok, "hist_ms": st.hist_ms})
            row = None if first_seen else self.approach_row(st, float(price), m, now)
            if row:
                near.append(row)
            for b in breaks:
                events.append({"cg_id": cg_id, "symbol": sym.upper(), "name": name, "rank": m["rank"], "kind": b.kind,
                               "ts": now, "price": float(price), "level": float(b.level), "prev_high": float(b.prev_high),
                               "prev_high_ms": b.prev_high_ms, "market_cap": m["market_cap"], "volume": m["volume"],
                               "weex_symbol": self.weex_symbol(sym, float(price)),
                               "peak": max(float(b.level), float(price)), "peak_ms": now})
        self.top_ids = top
        self.approaching = sorted(near, key=lambda a: a["dist_pct"])
        await asyncio.to_thread(store.save_coins, self.db, coin_rows)
        await asyncio.to_thread(store.set_peaks, self.db, peaks)
        for cg_id in [c for c, v in self.tracked.items()             # stop following the old ones
                      if all(t["peak"] is not None and now > t["ts"] + self.track_ms for t in v)]:
            del self.tracked[cg_id]
        if events:
            ids = await asyncio.to_thread(store.add_events, self.db, events)
            for e, i in zip(events, ids):
                e["id"] = i
                self.tracked.setdefault(e["cg_id"], []).append(
                    {"id": i, "ts": now, "base": e["peak"], "peak": e["peak"], "peak_ms": now})
            await self.alert(events, now)
        await self.alert_approaching(now)
        self.last_scan_ms = now
        return events

    async def alert(self, events: list[dict], now: int) -> None:
        h = self.h
        send = [e for e in events if (e["volume"] or 0) >= float(h.alert_min_volume_usd)]
        skipped = [e["id"] for e in events if e not in send]
        if skipped:
            await asyncio.to_thread(store.set_alert_status, self.db, skipped, "skipped")
        if not send or not h.telegram:
            return
        send.sort(key=lambda e: (e["kind"] != ATH, e["rank"] or 10 ** 6))
        n = int(h.max_lines_per_alert)
        for i in range(0, len(send), n):
            chunk = send[i:i + n]
            text = alert_text(chunk, now, int(h.top_n), self.cfg.dashboard.base_url, len(send) > n)
            msg_id = await self.eng.tg.send(text, dedupe_key="highs:" + ",".join(str(e["id"]) for e in chunk),
                                           event="highs")
            status = "sent" if msg_id else ("console" if not self.eng.tg.enabled else "failed")
            await asyncio.to_thread(store.set_alert_status, self.db, [e["id"] for e in chunk], status)

    async def scan_loop(self) -> None:
        while True:
            try:
                evs = await self.scan()
                self.last_error = None
                log.info("highs: scanned %d coins, %d new breaks", len(self.top_ids), len(evs))
            except Exception as e:  # noqa: BLE001 - keep scanning; the tab shows the error
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("highs scan failed: %s", self.last_error)
            await asyncio.sleep(float(self.h.scan_min) * 60)

    # ---- one year of candles per coin (for the 52-week high) ------------------------------
    def needs_history(self, now: int) -> list[str]:
        out = []
        for cg_id in self.top_ids:
            st = self.states.get(cg_id)
            if st is None:
                continue
            last = st.hist[-1][0] if st.hist else 0
            gap = st.hist[-2][0] if len(st.hist) > 1 else 0
            if not st.hist_ok or (last - gap > STALE_HIST and now - st.hist_ms > DAY):
                out.append(cg_id)
        return out

    async def history_loop(self) -> None:
        while True:
            todo = self.needs_history(self.eng.clock.now_ms()) if self.top_ids else []
            if not todo:
                await asyncio.sleep(300)
                continue
            cg_id = todo[0]
            try:
                candles = await self.cg.ohlc_year(cg_id)
            except Exception as e:  # noqa: BLE001
                log.warning("highs: candles for %s failed: %s", cg_id, e)
                await asyncio.sleep(60)
                continue
            now = self.eng.clock.now_ms()
            st = self.states[cg_id]
            merge_candles(st, candles if isinstance(candles, list) else [], now)
            m = self.meta.get(cg_id, {})
            await asyncio.to_thread(store.save_coins, self.db, [{
                "cg_id": cg_id, "symbol": st.symbol, "name": st.name, "rank": m.get("rank"), "price": m.get("price"),
                "market_cap": m.get("market_cap"), "volume": m.get("volume"), "ath": st.ath, "ath_ms": st.ath_ms,
                "hist": st.hist, "hist_ok": st.hist_ok, "hist_ms": st.hist_ms,
                "updated_ms": m.get("updated_ms") or now}])

    # ---- dashboard -------------------------------------------------------------------------
    def view(self) -> dict:
        h = self.h
        now = self.eng.clock.now_ms()
        evs = store.events_since(self.db, now - int(h.show_days) * DAY)
        out = []
        for e in evs:
            m = self.meta.get(e.cg_id, {})
            cur = m.get("price")
            out.append({"id": e.id, "cg_id": e.cg_id, "symbol": e.symbol, "name": e.name, "kind": e.kind,
                        "rank": m.get("rank") or e.rank, "ts": e.ts, "price_at_break": e.price, "level": e.level,
                        "prev_high": e.prev_high, "prev_high_ms": e.prev_high_ms, "price": cur,
                        "since_break_pct": (cur / e.price - 1) * 100 if cur else None,
                        "vs_old_high_pct": (cur / e.prev_high - 1) * 100 if cur and e.prev_high else None,
                        "peak": e.peak, "peak_ms": e.peak_ms,
                        "runup_pct": (e.peak / e.prev_high - 1) * 100 if e.peak and e.prev_high else None,
                        "runup_from_alert_pct": (e.peak / e.price - 1) * 100 if e.peak and e.price else None,
                        "off_peak_pct": (min(cur / e.peak, 1.0) - 1) * 100 if cur and e.peak else None,
                        "market_cap": m.get("market_cap") or e.market_cap, "volume": m.get("volume") or e.volume,
                        "weex_symbol": e.weex_symbol, "alert": e.alert_status})
        ready = sum(1 for c in self.top_ids if self.states.get(c) and self.states[c].hist_ok)
        study = None
        p = Path(self.cfg.data_dir) / h.study_file
        if not p.exists():             # not run on this machine yet: the snapshot shipped with the code
            p = Path(__file__).with_name("study_snapshot.json")
        if p.exists():
            try:
                study = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                study = None
        return {"events": out, "study": study, "approaching": self.approaching,
                "status": {"approach_pct": float(h.get("approach_pct", 3)), "plan_target_pct": self.plan[0],
                           "plan_stop_pct": self.plan[1], "top_n": int(h.top_n), "tracked": len(self.top_ids), "history_ready": ready,
                           "last_scan_ms": self.last_scan_ms, "scan_min": float(h.scan_min), "keyed": self.keyed,
                           "error": self.last_error, "min_high_age_days": float(h.min_high_age_days),
                           "show_days": int(h.show_days), "peak_track_days": self.track_ms / DAY,
                           "peaks_pending": sum(1 for v in self.tracked.values() for t in v if t["peak"] is None)}}


def approach_text(rows: list[dict], now: int, plan: tuple[float, float], base_url: str, more: bool) -> str:
    lines = ["🎯 Approaching a high · a buy-stop order at the level catches the break"]
    for a in rows:
        what = "ATH" if a["kind"] == ATH else "52W high"
        rank = f" #{a['rank']}" if a["rank"] else ""
        weex = f" · WEEX {a['weex_symbol']}" if a.get("weex_symbol") else ""
        lines.append(f"{a['symbol']}{rank} {fmt_price(a['price'])} → {what} {fmt_price(a['level'])} "
                     f"({a['dist_pct']:.1f}% away, set {age_text(a['level_ms'], now)}) · vol {fmt_usd(a['volume'])}{weex}")
        lines.append(f"   target +{plan[0]:g}% {fmt_price(a['target'])} · stop -{plan[1]:g}% {fmt_price(a['stop'])}")
    if more:
        lines.append("(continued in the next message)")
    lines.append("Levels are CoinGecko prices: check the level on the exchange chart before placing an order.")
    lines.append(f"📋 {base_url.rstrip('/')}/highs")
    return "\n".join(lines)


def alert_text(events: list[dict], now: int, top_n: int, base_url: str, more: bool) -> str:
    lines = [f"🏔️ New highs · CoinGecko top {top_n:,}"]
    for e in events:
        icon = "🚀 ATH" if e["kind"] == ATH else "📈 52W"
        what = "old ATH" if e["kind"] == ATH else "old 52w high"
        rank = f" #{e['rank']}" if e["rank"] else ""
        weex = f" · WEEX {e['weex_symbol']}" if e.get("weex_symbol") else ""
        lines.append(f"{icon} {e['symbol']}{rank} {fmt_price(e['price'])} · {what} {fmt_price(e['prev_high'])} "
                     f"({age_text(e['prev_high_ms'], now)}) · vol {fmt_usd(e['volume'])}{weex}")
    if more:
        lines.append("(continued in the next message)")
    lines.append(f"📋 {base_url.rstrip('/')}/highs")
    return "\n".join(lines)
