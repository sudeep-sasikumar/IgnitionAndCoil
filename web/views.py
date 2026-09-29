"""JSON views of engine state for the dashboard."""
from __future__ import annotations

import time

import numpy as np

from data.bars import BarArrays
from data.db import clean_json
from features import indicators as ind
from levels.resistance import build_levels
from notify.messages import tv_link


def header(eng) -> dict:
    r = eng.regime
    return clean_json({
        "regime": r.state if r else None,
        "btc_dump": r.btc_dump if r else False,
        "btc_price": r.btc_price if r else None,
        "btc_ret_1h": r.btc_ret_1h if r else None,
        "btc_above_ema50": r.btc_above_ema50 if r else None,
        "btc_ema50_rising": r.btc_ema50_rising if r else None,
        "breadth": r.breadth if r else None,
        "breadth_n": r.breadth_n if r else 0,
        "starting": eng.starting,
        "default_stop_policy": str(eng.cfg.trade.get("default_stop_policy", "L")).upper(),
        "data_ok": eng.healthy,
        "ws_ok": eng.ws.connected,
        "last_scan_ms": eng.last_scan_ms or None,
        "last_bar_ms": eng.last_bar_ms or None,
        "universe_n": len(eng.universe.symbols),
        "paused": eng.paused,
        "watch_muted": eng.watch_muted,
        "telegram": eng.tg.enabled,
        "oi_history_min": eng.oi.history_minutes(eng.btc),
        **trade_counts(eng),
    })


def trade_counts(eng) -> dict:
    from sqlalchemy import func, select
    from data.trade_db import ManualTradeRow
    with eng.db.session() as s:
        rows = s.execute(select(ManualTradeRow.status, func.count()).group_by(ManualTradeRow.status)).all()
    c = dict(rows)
    return {"open_trades": c.get("OPEN", 0) + c.get("NEEDS_CONFIRMATION", 0),
            "needs_confirmation": c.get("NEEDS_CONFIRMATION", 0), "open_paper": len(eng.paper.open)}


def paper_list(eng, signal_id: str | None, status: str | None, limit: int) -> list[dict]:
    from sqlalchemy import select
    from data.trade_db import PaperTradeRow
    with eng.db.session() as s:
        q = select(PaperTradeRow).order_by(PaperTradeRow.entry_ms.desc()).limit(limit)
        if signal_id:
            q = q.where(PaperTradeRow.signal_id == signal_id)
        if status:
            q = q.where(PaperTradeRow.status == status.upper())
        out = []
        for r in s.execute(q).scalars():
            pos = eng.paper.open.get(r.id)
            px = eng.tape.last_price.get(r.symbol) or eng.premium.mark(r.symbol)
            st = pos.state if pos else None
            out.append({"id": r.id, "signal_id": r.signal_id, "policy": r.policy, "symbol": r.symbol,
                        "setup": r.setup, "score": r.score, "regime": r.regime, "session": r.session,
                        "suppressed_reason": r.suppressed_reason, "status": r.status, "entry_ms": r.entry_ms,
                        "entry_ref": r.entry_ref, "entry_fill": r.entry_fill, "initial_stop": r.initial_stop,
                        "tp1": r.tp1, "tp2": r.tp2, "risk_usd": r.risk_usd, "exit_ms": r.exit_ms,
                        "exit_reason": r.exit_reason, "net_pnl": r.net_pnl, "funding_usd": r.funding_usd,
                        "r_multiple": r.r_multiple, "mfe_pct": r.mfe_pct, "mae_pct": r.mae_pct,
                        "tp1_ms": r.tp1_ms, "tp2_ms": r.tp2_ms, "legs": r.legs,
                        "current_stop": st.stop if st else None, "phase": st.phase if st else None,
                        "price": px if r.status == "OPEN" else None})
        return clean_json(out)


def row(r, precision: int | None = None) -> dict:
    f = r.features
    return {
        "symbol": r.symbol, "price": r.price, "precision": precision, "state": r.state, "score": r.score, "setup": r.setup,
        "n_pass": r.n_pass, "n_conds": r.n_conds, "failed": r.failed,
        "ret_1h": f.ret_1h, "ret_4h": f.ret_4h, "ret_24h": f.ret_24h,
        "rvol_5m": f.rvol_5m, "rvol_15m": f.rvol_15m, "rs_1h": f.rs_1h,
        "oi_chg_1h": f.oi_chg_1h, "oi_chg_4h": f.oi_chg_4h,
        "funding_8h": f.funding_8h * 100 if f.funding_8h is not None else None,
        "taker": f.taker_buy_ratio_15m, "cvd": f.cvd_slope_1h_norm,
        "headroom_pct": r.headroom_pct, "price_discovery": r.price_discovery,
        "wicks": f.deep_wicks_24h, "bbw_pct": f.bbw_pct_1h, "atr_1h_pct": f.atr_1h_pct,
        "warm": f.warm,
    }


def signal_summary(s) -> dict:
    return {"signal_id": s.signal_id, "symbol": s.symbol, "setup": s.setup, "score": s.score,
            "bar_close_ms": s.bar_close_ms, "regime": s.regime, "session": s.session,
            "alert_status": s.alert_status, "suppressed_reason": s.suppressed_reason}


def state(eng) -> dict:
    since = int(time.time() * 1000) - 7 * 86_400_000
    sigs = [signal_summary(s) for s in reversed(eng.db.recent_signals(since))][:30]
    return {"type": "state", "header": header(eng), "rows": clean_json([row(r, eng.precision(r.symbol)) for r in eng.scan_rows.values()]),
            "signals": sigs}


def prices(eng) -> dict:
    out = {}
    for sym in eng.tracked:
        last = eng.tape.last_price.get(sym)
        mark = eng.premium.mark(sym)
        if last or mark:
            out[sym] = {"last": last, "mark": mark}
    # header rides along so connection/data status never lags behind a full state push
    return {"type": "prices", "ts": eng.clock.now_ms(), "data": out, "header": header(eng)}


def signal_detail(eng, signal_id: str) -> dict | None:
    s = eng.db.get_signal(signal_id)
    if s is None:
        return None
    d = dict(s.data)
    d.update(signal_summary(s))
    d["telegram_msg_id"] = s.telegram_msg_id
    d["config_hash"] = s.config_hash
    d["precision"] = eng.precision(s.symbol)
    d["tv_link"] = tv_link(s.symbol, eng.cfg)
    return d


def symbol_detail(eng, sym: str) -> dict | None:
    r = eng.scan_rows.get(sym)
    if r is None:
        return None
    w = r.watch
    return clean_json({
        **row(r, eng.precision(sym)),
        "ignition_conds": r.ignition_conds, "watch_conds": r.watch_conds,
        "coil_entry_conds": r.coil_entry_conds, "breakdown": r.breakdown,
        "watch": {"box_high": w.box_high, "box_low": w.box_low, "since_ms": w.since_ms} if w else None,
        "features": r.features.as_dict(),
    })


# ---- chart -----------------------------------------------------------------------

def _line(t_sec: np.ndarray, v: np.ndarray) -> list[dict]:
    return [{"time": int(a), "value": float(b)} for a, b in zip(t_sec, v) if np.isfinite(b)]


def vwap_series(b5: BarArrays, closes_ms: np.ndarray, n: int) -> np.ndarray:
    """Rolling n-bar 5m VWAP evaluated at each given close time (closed 5m bars only)."""
    tp = (b5.h + b5.l + b5.c) / 3.0
    pv = np.concatenate([[0.0], np.cumsum(tp * b5.v)])
    vv = np.concatenate([[0.0], np.cumsum(b5.v)])
    out = np.full(len(closes_ms), np.nan)
    for i, t in enumerate(closes_ms):
        k = int(np.searchsorted(b5.tc, t, side="right"))
        j = max(0, k - n)
        if k - j >= 12 and vv[k] > vv[j]:
            out[i] = (pv[k] - pv[j]) / (vv[k] - vv[j])
    return out


_chart_cache: dict[str, tuple[float, dict]] = {}


async def _arrays(eng, sym: str) -> dict[str, BarArrays]:
    if eng.store.has(sym) and len(eng.store.series(sym, "15m")):
        return {tf: eng.store.series(sym, tf).arrays() for tf in ("5m", "15m", "1h", "4h")}
    out = {}
    for tf, n in (("5m", 1000), ("15m", 400), ("1h", 1000), ("4h", 400)):
        out[tf] = BarArrays.from_bars(await eng.rest.klines(sym, tf, n))
    return out


async def chart_data(eng, sym: str) -> dict:
    hit = _chart_cache.get(sym)
    tracked = eng.store.has(sym)
    if hit and not tracked and time.time() - hit[0] < 60:
        return hit[1]
    a = await _arrays(eng, sym)
    b15 = a["15m"]
    n = eng.cfg.dashboard.chart_bars_15m
    if not len(b15):
        return {"symbol": sym, "candles": []}
    f = eng.cfg.features
    ema20 = ind.ema(b15.c, f.ema_fast)
    ema50 = ind.ema(b15.c, f.ema_slow)
    vw = vwap_series(a["5m"], b15.tc, f.vwap_bars_5m)
    t = b15.t // 1000
    sl = slice(-n, None)
    levels = build_levels(a["1h"], a["4h"], eng.cfg)
    last = float(b15.c[-1])
    lv = [{"price": x.price, "kind": x.kind} for x in levels.levels if last * 0.85 <= x.price <= last * 1.2]
    w = eng.signals.coil.watches.get(sym)
    out = clean_json({
        "symbol": sym, "precision": eng.precision(sym), "tracked": tracked, "tv_link": tv_link(sym, eng.cfg),
        "candles": [{"time": int(ti), "open": float(o), "high": float(h), "low": float(l), "close": float(c)}
                    for ti, o, h, l, c in zip(t[sl], b15.o[sl], b15.h[sl], b15.l[sl], b15.c[sl])],
        "ema20": _line(t[sl], ema20[sl]), "ema50": _line(t[sl], ema50[sl]), "vwap": _line(t[sl], vw[sl]),
        "levels": lv,
        "watch": {"box_high": w.box_high, "box_low": w.box_low, "since_ms": w.since_ms} if w else None,
    })
    _chart_cache[sym] = (time.time(), out)
    return out
