"""Action alerts for your logged trades (spec §10.2). Plain text; sent as replies to the
original signal message (or threaded under the first message for your own trades)."""
from __future__ import annotations

from notify.messages import fmt_price


def _head(t: dict) -> str:
    src = t["signal_id"] or t["trade_ref"]
    return f"{t['symbol']} · {src}"


def tracking(t: dict, p: int | None, link: str, late_summary: list[str] | None = None) -> str:
    src = t["signal_id"] or f"{t['trade_ref']} (own trade)"
    lines = [f"📌 Tracking {src}: {t['symbol']} entry {fmt_price(t['entry_price'], p)}, {t['leverage']:g}x, "
             f"liq ~{fmt_price(t['liq_price'], p)}. Set your SL on WEEX at {fmt_price(t['current_stop'], p)} now."]
    tp = f"TP1 {fmt_price(t['tp1'], p)}" if t.get("tp1") else ""
    if t.get("tp2"):
        tp += f" | TP2 {fmt_price(t['tp2'], p)}"
    if tp:
        lines.append(tp)
    for w in t.get("warnings") or []:
        lines.append(f"⚠️ {w}")
    if late_summary:
        lines.append("Logged late - the plan so far:")
        lines += [f"• {s}" for s in late_summary]
    lines.append(f"📋 {link}")
    return "\n".join(lines)


def tp1(t: dict, frac_pct: float, price: float, be: float, p: int | None) -> str:
    return (f"🎯 {_head(t)}: TP1 reached. Close {frac_pct:g}% at ~{fmt_price(price, p)}. "
            f"Move SL to breakeven + fees: {fmt_price(be, p)}.")


def tp2(t: dict, frac_pct: float, price: float, runner_pct: float, p: int | None) -> str:
    return (f"🎯 {_head(t)}: TP2 reached. Close {frac_pct:g}% at ~{fmt_price(price, p)}. "
            f"Runner {runner_pct:g}% stays on the trail.")


def trail(t: dict, stop: float, p: int | None) -> str:
    return f"↗️ {_head(t)}: Move SL to {fmt_price(stop, p)}."


EXIT_TEXT = {
    "EMA_EXIT": "15m close below EMA20",
    "TIME_STOP": "time stop (+1% not reached in 45 min)",
    "MAX_HOLD": "max hold reached",
    "TP2": "all targets done",
}


def close_remaining(t: dict, reason: str, p: int | None, stop: float | None = None) -> str:
    why = EXIT_TEXT.get(reason) or (f"plan stop {fmt_price(stop, p)} hit" if stop is not None else reason)
    return f"🔚 {_head(t)}: {why}. Close remaining at market."


def stop_hit(t: dict, stop: float, p: int | None, offline: bool = False) -> str:
    when = " while the app was offline" if offline else ""
    return (f"🛑 {_head(t)}: Price hit your SL level {fmt_price(stop, p)}{when}. "
            f"Log your exit on the dashboard.")


def liquidation(t: dict, liq: float, p: int | None) -> str:
    return (f"💥 {_head(t)}: mark price reached the estimated liquidation price {fmt_price(liq, p)}. "
            f"Check WEEX and log what happened.")


def chase(t: dict, chase_limit: float, p: int | None) -> str:
    return (f"⚠️ {_head(t)}: your entry {fmt_price(t['entry_price'], p)} is above the don't-enter-above price "
            f"{fmt_price(chase_limit, p)}. Allowed, but flagged.")


def offline_crossing(t: dict, items: list[str]) -> str:
    return f"⚠️ {_head(t)}: while the app was offline:\n" + "\n".join(f"• {i}" for i in items) + \
        "\nConfirm on the dashboard."
