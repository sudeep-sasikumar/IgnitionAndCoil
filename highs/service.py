"""Highs tab service: scans the CoinGecko top N every few minutes, detects 52-week-high and
all-time-high breaks (highs.detector), records them, alerts on Telegram and feeds the dashboard.

Two loops share one paced CoinGecko client:
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
from highs.detector import ATH, DAY, CoinState, Obs, check, excluded, merge_candles, update

log = logging.getLogger("highs")
STALE_HIST = 5 * DAY          # reload candles after a gap in our own daily observations (candles are 4-day)


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

    def load(self) -> None:
        for cg_id, (st, row) in store.load_states(self.db).items():
            self.states[cg_id] = st
            self.meta[cg_id] = {"rank": row.rank, "price": row.price, "market_cap": row.market_cap,
                                "volume": row.volume}
        log.info("highs: %d coins loaded from the database", len(self.states))

    def weex_symbol(self, symbol: str) -> str | None:
        s = f"{symbol.upper()}USDT"
        return s if s in self.eng.universe.exchange_symbols else None

    # ---- scanning ---------------------------------------------------------------------------
    async def scan(self) -> list[dict]:
        h = self.h
        now = self.eng.clock.now_ms()
        rows = await self.cg.top(int(h.top_n))
        events: list[dict] = []
        coin_rows: list[dict] = []
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
            update(st, ob, now)
            st.symbol, st.name = sym, name
            self.states[cg_id] = st
            m = {"rank": r.get("market_cap_rank"), "price": float(price), "market_cap": r.get("market_cap"),
                 "volume": r.get("total_volume")}
            self.meta[cg_id] = m
            coin_rows.append({"cg_id": cg_id, "symbol": sym, "name": name, **m, "ath": st.ath, "ath_ms": st.ath_ms,
                              "hist": st.hist, "hist_ok": st.hist_ok, "hist_ms": st.hist_ms, "updated_ms": now})
            for b in breaks:
                events.append({"cg_id": cg_id, "symbol": sym.upper(), "name": name, "rank": m["rank"], "kind": b.kind,
                               "ts": now, "price": float(price), "level": float(b.level), "prev_high": float(b.prev_high),
                               "prev_high_ms": b.prev_high_ms, "market_cap": m["market_cap"], "volume": m["volume"],
                               "weex_symbol": self.weex_symbol(sym)})
        self.top_ids = top
        await asyncio.to_thread(store.save_coins, self.db, coin_rows)
        if events:
            ids = await asyncio.to_thread(store.add_events, self.db, events)
            for e, i in zip(events, ids):
                e["id"] = i
            await self.alert(events, now)
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
                "hist": st.hist, "hist_ok": st.hist_ok, "hist_ms": st.hist_ms, "updated_ms": now}])

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
        return {"events": out, "study": study,
                "status": {"top_n": int(h.top_n), "tracked": len(self.top_ids), "history_ready": ready,
                           "last_scan_ms": self.last_scan_ms, "scan_min": float(h.scan_min), "keyed": self.keyed,
                           "error": self.last_error, "min_high_age_days": float(h.min_high_age_days),
                           "show_days": int(h.show_days)}}


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
