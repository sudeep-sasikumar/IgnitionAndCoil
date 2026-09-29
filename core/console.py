"""M1 console output: regime header + feature table."""
from __future__ import annotations

import math

from features.compute import Features
from signals.regime import Regime


def _f(x, fmt: str, width: int = 7) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-".rjust(width)
    return format(x, fmt).rjust(width)


def header(clock, regime: Regime | None, universe_n: int, ws_ok: bool | None, weight: int, healthy: bool,
           oi_minutes: float) -> str:
    t = clock.fmt(clock.now_ms(), with_date=True)
    if regime is None:
        return f"[{t} London] regime: n/a (warming)"
    dump = " BTC_DUMP" if regime.btc_dump else ""
    return (
        f"\n=== {t} London | {regime.state}{dump} | BTC {regime.btc_price:,.1f} "
        f"({regime.btc_ret_1h:+.2f}% 1h, {'>' if regime.btc_above_ema50 else '<'}EMA50 "
        f"{'rising' if regime.btc_ema50_rising else 'falling'}, 45m {regime.btc_ret_15m_x3:+.2f}%) | "
        f"breadth {regime.breadth:.0f}% of {regime.breadth_n} | universe {universe_n} | "
        f"WS {'n/a' if ws_ok is None else 'OK' if ws_ok else 'DOWN'} | data {'OK' if healthy else 'STALE'} | "
        f"weight {weight}/10s | OI history {oi_minutes:.0f} min ==="
    )


COLS = ("symbol", "state", "score", "pass", "room%", "price", "r1h%", "r4h%", "r24h%", "rs1h", "rvol5", "rvol15",
        "vwap%", "ema15", "atr1h%", "bbw%ile", "oi1h%", "oi4h%", "fund8h%", "taker", "cvd", "wick")


def table(rows: list[Features], scan: dict, top_n: int) -> str:
    def key(r: Features):
        s = scan.get(r.symbol)
        rank = {"ENTRY": 2, "WATCH": 1}.get(s.state, 0) if s else 0
        return (rank, s.score if s else -1, r.rs_1h if not math.isnan(r.rs_1h) else -1e9)

    rows = sorted(rows, key=key, reverse=True)[:top_n]
    lines = ["  ".join(c.rjust(7) if i else c.ljust(14) for i, c in enumerate(COLS))]
    for r in rows:
        vw = (r.price / r.vwap_24h - 1) * 100 if r.vwap_24h else float("nan")
        trend = "up" if r.ema20_15m > r.ema50_15m else "down"
        fund = r.funding_8h * 100 if r.funding_8h is not None else None
        s = scan.get(r.symbol)
        state = s.state if s else "-"
        score = s.score if s else None
        passed = f"{s.n_pass}/{s.n_conds}{s.setup[0]}" if s else "-"
        room = f"{s.headroom_pct:.1f}{'d' if s.price_discovery else ''}" if s else "-"
        lines.append("  ".join([
            (r.symbol + ("*" if not r.warm else "")).ljust(14),
            state.rjust(7), _f(score, "d", 7), passed.rjust(7), room.rjust(7),
            _f(r.price, ".6g", 7), _f(r.ret_1h, "+.2f", 7), _f(r.ret_4h, "+.2f", 7), _f(r.ret_24h, "+.1f", 7),
            _f(r.rs_1h, "+.2f", 7), _f(r.rvol_5m, ".1f", 7), _f(r.rvol_15m, ".1f", 7), _f(vw, "+.2f", 7),
            trend.rjust(7), _f(r.atr_1h_pct, ".2f", 7), _f(r.bbw_pct_1h, ".0f", 7),
            _f(r.oi_chg_1h, "+.2f", 7), _f(r.oi_chg_4h, "+.2f", 7), _f(fund, "+.4f", 7),
            _f(r.taker_buy_ratio_15m, ".2f", 7), _f(r.cvd_slope_1h_norm, "+.2f", 7), _f(r.deep_wicks_24h, "d", 7),
        ]))
    lines.append("(ENTRY/WATCH first, then by score; pass = conditions met, I=Ignition C=Coil; room d = price "
                 "discovery; * = warming up; oi% blank until enough OI history is recorded)")
    return "\n".join(lines)
