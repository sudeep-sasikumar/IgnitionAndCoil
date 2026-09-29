"""Telegram message text (plain text, no parse mode - nothing to escape)."""
from __future__ import annotations

import json
import math
from pathlib import Path

from signals.setups import COIL, IGNITION

SETUP_TITLE = {IGNITION: "🚀 IGNITION", COIL: "🌀 COIL BREAKOUT"}
BREAKDOWN_LABEL = {"rvol": "RVOL", "rs": "RS", "oi": "OI", "flow": "Flow", "headroom": "Room",
                   "regime": "Regime", "funding": "Fund", "wick": "Wick"}


def fmt_price(x: float | None, precision: int | None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    if precision is None:
        precision = max(2, 5 - int(math.floor(math.log10(abs(x))))) if x else 2
    return f"{x:,.{max(precision, 0)}f}"


def fmt_duration(ms: int) -> str:
    m = max(0, ms) // 60_000
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def tv_symbol(symbol: str, cfg) -> str:
    tv = cfg.tradingview
    overrides = tv.tv_overrides.to_dict() if hasattr(tv.tv_overrides, "to_dict") else (tv.tv_overrides or {})
    if symbol in overrides:
        return overrides[symbol]
    missing_file = Path(cfg.data_dir) / tv.tv_missing_file
    try:
        missing = set(json.loads(missing_file.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        missing = set()
    tmpl = tv.tv_fallback_template if symbol in missing else tv.tv_symbol_template
    return tmpl.format(symbol=symbol)


def tv_link(symbol: str, cfg) -> str:
    return cfg.tradingview.chart_url.format(tv_symbol=tv_symbol(symbol, cfg))


def dashboard_link(signal_id: str, cfg) -> str:
    return f"{cfg.dashboard.base_url.rstrip('/')}/signal/{signal_id}"


def _stop_line(sp: dict, precision: int | None, mine: bool = False) -> str:
    tag = " ◀ your stop" if mine else ""
    if sp["price"] is None:
        return f"Stop {sp['policy']} n/a ({sp['note']}){tag}"
    return (f"Stop {sp['policy']} {fmt_price(sp['price'], precision)} ({sp['pct']:+.2f}%) → "
            f"-${abs(sp['loss_usd']):.1f} | BE win rate {sp['be_winrate'] * 100:.0f}%{tag}")


def entry_message(sig: dict, now_ms: int, cfg, precision: int | None, already_holding: bool = False) -> str:
    """sig = Signal.to_record() (plus signal_id)."""
    p = sig["plan"]
    f = sig["features"]
    pr = precision
    regime = sig["regime"].get("state", "?")
    lines = [f"{SETUP_TITLE.get(sig['setup'], sig['setup'])} — {sig['symbol']} | score {sig['score']} | "
             f"{regime} | #{sig['signal_id']}"]
    flags = []
    if already_holding:
        flags.append("⚠️ you already hold this")
    flags += [t for t in sig["tags"] if t in ("PRICE_DISCOVERY", "RISK_OFF", "WEEKEND") or t.startswith("BLACKOUT")]
    if flags:
        lines.append(" | ".join(flags))
    lines.append(f"Entry ~{fmt_price(p['ref_entry'], pr)} | Don't enter above {fmt_price(p['chase_limit'], pr)} | "
                 f"Liq ~{fmt_price(p['liq_price'], pr)} ({p['leverage']:g}x, mark)")
    mine = str(cfg.trade.get("default_stop_policy", "L")).upper()
    order = ("stop_s", "stop_l") if mine == "S" else ("stop_l", "stop_s")
    for key in order:
        lines.append(_stop_line(p[key], pr, mine=p[key]["policy"] == mine))
    tp1 = f"TP1 {fmt_price(p['tp1'], pr)} (+{p['tp1_pct']:g}%, close {p['tp1_close_pct']:g}%)"
    if p["tp2"] is not None:
        tp2 = f"TP2 {fmt_price(p['tp2'], pr)} (+{p['tp2_pct']:.1f}%, close {p['tp2_close_pct']:g}%)"
    else:
        tp2 = "TP2 dropped (resistance too close)"
    lines.append(f"{tp1} | {tp2}")
    lines.append(f"Runner {p['runner_pct']:g}%: chandelier / 15m swing-low trail, stop only moves up")

    ign = sig["setup"] == IGNITION
    rvol = f.get("rvol_5m") if ign else f.get("rvol_15m")
    oi = f.get("oi_chg_1h") if ign else f.get("oi_chg_4h")
    oi_s = f"OI {oi:+.1f}% {'1h' if ign else '4h'}" if oi is not None else "OI n/a"
    fund = f.get("funding_8h")
    nxt = f.get("next_funding_ms")
    fund_s = f"Fund {fund * 100:.3f}%" if fund is not None else "Fund n/a"
    if nxt:
        fund_s += f" (next in {fmt_duration(nxt - now_ms)})"
    room = sig["headroom"]
    room_s = f"Room {room['pct']:.1f}%" + (" (discovery)" if room.get("price_discovery") else "")
    lines.append(f"RVOL {rvol:.1f}x | RS {f.get('rs_1h', 0):+.1f}% | {oi_s} | Taker {f.get('taker_buy_ratio_15m', 0):.2f} "
                 f"| {fund_s} | {room_s}")
    bd = " · ".join(f"{BREAKDOWN_LABEL.get(k, k)} {v:+g}" if k == "wick" else f"{BREAKDOWN_LABEL.get(k, k)} {v:g}"
                    for k, v in sig["breakdown"].items() if not (k == "wick" and v == 0))
    lines.append(f"Score: {bd}")
    for w in p.get("warnings") or []:
        lines.append(f"⚠️ {w}")
    lines.append(f"📈 TradingView: {tv_link(sig['symbol'], cfg)}")
    lines.append(f"📋 Log trade: {dashboard_link(sig['signal_id'], cfg)}")
    return "\n".join(lines)


def watch_message(symbol: str, box_high: float, box_low: float, f, precision: int | None, cfg) -> str:
    oi = f"{f.oi_chg_4h:+.1f}%" if f.oi_chg_4h is not None else "n/a"
    return (f"👀 WATCH — {symbol} coiling | box {fmt_price(box_low, precision)}–{fmt_price(box_high, precision)} | "
            f"BBW pct {f.bbw_pct_1h:.0f} | OI {oi} 4h | ret4h {f.ret_4h:+.1f}%\n"
            f"Entry trigger: 15m close above {fmt_price(box_high, precision)} with volume (valid {cfg.coil.watch_valid_h}h)\n"
            f"📈 {tv_link(symbol, cfg)}")
