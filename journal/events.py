"""Your logged events -> effective trade state.

manual_trade_events is append-only. Rows are never changed: an edit is a new EDIT row that
points at its target (new = {"event_id": id, <changed fields>}, old = previous values); a
mistaken entry is withdrawn with a VOID row. The effective legs / stop / open quantity are
always rebuilt from the full log, so the audit trail and the numbers can never disagree.
"""
from __future__ import annotations

ACTION_KINDS = ("PARTIAL_CLOSE", "CLOSE", "STOP_MOVE")


def effective_events(rows: list) -> list[dict]:
    """rows: ManualTradeEventRow-like objects in id order. Returns action events with edits
    applied (VOIDed ones flagged)."""
    evs: dict[int, dict] = {}
    for r in rows:
        if r.kind in ACTION_KINDS:
            new = r.new or {}
            evs[r.id] = {"id": r.id, "kind": r.kind, "ts": int(r.ts), "price": r.price, "qty": r.qty,
                         "order": new.get("order", "market"), "note": r.note, "void": False}
    for r in rows:
        target = (r.new or {}).get("event_id")
        if target not in evs:
            continue
        if r.kind == "EDIT":
            for k in ("ts", "price", "qty", "order"):
                if k in r.new:
                    evs[target][k] = int(r.new[k]) if k == "ts" else r.new[k]
        elif r.kind == "VOID":
            evs[target]["void"] = True
    return sorted(evs.values(), key=lambda e: (e["ts"], e["id"]))


def rebuild(qty: float, initial_stop: float, entry_ms: int, rows: list) -> dict:
    evs = [e for e in effective_events(rows) if not e["void"]]
    legs, stops = [], [(entry_ms, initial_stop)]
    closed = 0.0
    for e in evs:
        if e["kind"] == "STOP_MOVE":
            stops.append((e["ts"], float(e["price"])))
        else:
            q = qty - closed if e["kind"] == "CLOSE" else min(float(e["qty"]), qty - closed)
            if q <= 0:
                continue
            closed += q
            legs.append({"ts": e["ts"], "price": float(e["price"]), "qty": q, "order": e["order"],
                         "kind": e["kind"], "event_id": e["id"]})
    stops.sort()
    return {"legs": legs, "qty_open": max(qty - closed, 0.0), "current_stop": stops[-1][1],
            "stop_timeline": stops, "events": evs}


def stop_at(timeline: list[tuple[int, float]], ts: int) -> float:
    cur = timeline[0][1]
    for t, s in timeline:
        if t <= ts:
            cur = s
    return cur
