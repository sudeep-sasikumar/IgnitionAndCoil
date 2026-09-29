"""Trade statistics (spec §8.3). Pure functions over trade dicts with at least
{net, r, entry_ms, exit_ms}; shared by the live stats page and the backtest report."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def metrics(trades: list[dict]) -> dict:
    ts = sorted(trades, key=lambda t: (t.get("exit_ms") or 0, t.get("entry_ms") or 0))
    n = len(ts)
    if n == 0:
        return {"n": 0}
    nets = [float(t["net"]) for t in ts]
    rs = [float(t["r"]) for t in ts if t.get("r") is not None and math.isfinite(t["r"])]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    # max consecutive losses and max drawdown of the cumulative $ curve (by exit time)
    streak = best = 0
    equity = peak = dd = 0.0
    for x in nets:
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
        equity += x
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return {
        "n": n, "wins": len(wins), "losses": len(losses), "win_rate": len(wins) / n,
        "net": sum(nets), "expectancy_usd": sum(nets) / n,
        "avg_r": sum(rs) / len(rs) if rs else None, "expectancy_r": sum(rs) / len(rs) if rs else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else None),
        "avg_win": gross_win / len(wins) if wins else None, "avg_loss": -gross_loss / len(losses) if losses else None,
        "max_consec_losses": best, "max_drawdown_usd": dd,
    }


def breakdown(trades: list[dict], key) -> dict[str, dict]:
    groups: dict[str, list] = defaultdict(list)
    for t in trades:
        groups[str(key(t))].append(t)
    return {k: metrics(v) for k, v in sorted(groups.items())}


def score_bucket(score: int | None) -> str:
    if score is None:
        return "n/a"
    if score < 70:
        return "<70"
    if score < 75:
        return "70-74"
    if score < 80:
        return "75-79"
    if score < 90:
        return "80-89"
    return "90+"


def london_hour(ms: int, tz: str = "Europe/London") -> str:
    return f"{datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz)).hour:02d}"


DIMENSIONS = {
    "policy": lambda t: t.get("policy", "-"),
    "setup": lambda t: t.get("setup", "-"),
    "regime": lambda t: t.get("regime", "-"),
    "score": lambda t: score_bucket(t.get("score")),
    "session": lambda t: t.get("session", "-"),
    "hour": lambda t: london_hour(t["entry_ms"]),
    "alert": lambda t: "suppressed" if t.get("suppressed_reason") else "sent",
}
