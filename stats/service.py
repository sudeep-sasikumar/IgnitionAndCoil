"""Stats page data (spec §8.3): system baseline (paper, per policy) and your trades."""
from __future__ import annotations

import statistics
import time

from sqlalchemy import select

from data.db import SignalRow, clean_json
from data.trade_db import ManualTradeRow, PaperTradeRow
from stats.metrics import DIMENSIONS, breakdown, metrics


def paper_trades(db, since_ms: int = 0) -> list[dict]:
    with db.session() as s:
        q = select(PaperTradeRow).where(PaperTradeRow.status == "CLOSED", PaperTradeRow.entry_ms >= since_ms)
        return [{"id": r.id, "signal_id": r.signal_id, "policy": r.policy, "symbol": r.symbol, "setup": r.setup,
                 "score": r.score, "regime": r.regime, "session": r.session, "suppressed_reason": r.suppressed_reason,
                 "net": r.net_pnl, "r": r.r_multiple, "entry_ms": r.entry_ms, "exit_ms": r.exit_ms,
                 "exit_reason": r.exit_reason, "mfe_pct": r.mfe_pct, "mae_pct": r.mae_pct}
                for r in s.execute(q).scalars()]


def manual_trades(db, since_ms: int = 0) -> list[dict]:
    with db.session() as s:
        q = select(ManualTradeRow).where(ManualTradeRow.status == "CLOSED", ManualTradeRow.entry_ms >= since_ms)
        rows = list(s.execute(q).scalars())
        sig = {r.signal_id: r for r in s.execute(select(SignalRow)).scalars() if r.signal_id}
        out = []
        for r in rows:
            sg = sig.get(r.signal_id)
            out.append({"id": r.id, "trade_ref": r.trade_ref, "signal_id": r.signal_id, "source": r.source,
                        "symbol": r.symbol, "setup": sg.setup if sg else "OWN", "score": sg.score if sg else None,
                        "regime": sg.regime if sg else "-", "session": sg.session if sg else "-",
                        "net": r.net_pnl, "r": r.r_multiple, "entry_ms": r.entry_ms, "exit_ms": r.closed_ms,
                        "plan_pnl": r.plan_pnl, "entry_gap_pct": r.entry_gap_pct})
        return out


def compute(db, since_ms: int = 0) -> dict:
    paper = paper_trades(db, since_ms)
    mine = manual_trades(db, since_ms)
    by_policy = {p: [t for t in paper if t["policy"] == p] for p in ("L", "S")}
    system = {
        "overall": {p: metrics(v) for p, v in by_policy.items()},
        "breakdowns": {dim: {p: breakdown(v, fn) for p, v in by_policy.items()}
                       for dim, fn in DIMENSIONS.items() if dim != "policy"},
    }

    taken_ids = {t["signal_id"] for t in mine if t["signal_id"]}
    with db.session() as s:
        open_taken = {r[0] for r in s.execute(select(ManualTradeRow.signal_id).where(
            ManualTradeRow.signal_id.is_not(None)))}
    taken_ids |= open_taken
    taken_vs = {p: {"taken": metrics([t for t in v if t["signal_id"] in taken_ids]),
                    "not_taken": metrics([t for t in v if t["signal_id"] not in taken_ids])}
                for p, v in by_policy.items()}

    gaps = [t["entry_gap_pct"] for t in mine if t.get("entry_gap_pct") is not None]
    adher = [t["net"] - t["plan_pnl"] for t in mine if t.get("plan_pnl") is not None and t.get("net") is not None]
    my = {
        "overall": metrics(mine),
        "by_source": {"signal": metrics([t for t in mine if t["signal_id"]]),
                      "own": metrics([t for t in mine if not t["signal_id"]])},
        "breakdowns": {dim: breakdown(mine, fn) for dim, fn in DIMENSIONS.items()
                       if dim in ("setup", "regime", "score", "session", "hour")},
        "entry_gap": {"n": len(gaps), "avg_pct": statistics.fmean(gaps) if gaps else None,
                      "median_pct": statistics.median(gaps) if gaps else None,
                      "worst_pct": max(gaps) if gaps else None},
        "plan_adherence": {"n": len(adher), "total_usd": sum(adher) if adher else None,
                           "avg_usd": statistics.fmean(adher) if adher else None,
                           "better": sum(1 for a in adher if a > 0), "worse": sum(1 for a in adher if a < 0),
                           "note": "your actual net minus what the exit engine would have made on your entry"},
        "taken_vs_not_taken": taken_vs,
        "you_vs_system_on_taken": {
            "you": metrics([t for t in mine if t["signal_id"]]),
            **{f"system_{p}": metrics([t for t in v if t["signal_id"] in taken_ids]) for p, v in by_policy.items()}},
    }
    with db.session() as s:
        n_signals = len(list(s.execute(select(SignalRow.id).where(SignalRow.bar_close_ms >= since_ms))))
    return clean_json({"generated_ms": int(time.time() * 1000), "since_ms": since_ms, "signals": n_signals,
                       "system": system, "mine": my})


def day_summary(db, start_ms: int, end_ms: int) -> dict:
    paper = [t for t in paper_trades(db, 0) if start_ms <= (t["exit_ms"] or 0) < end_ms]
    mine = [t for t in manual_trades(db, 0) if start_ms <= (t["exit_ms"] or 0) < end_ms]
    return {"L": metrics([t for t in paper if t["policy"] == "L"]),
            "S": metrics([t for t in paper if t["policy"] == "S"]), "mine": metrics(mine)}
