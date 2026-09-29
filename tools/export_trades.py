"""Export trades to CSV (spec §8.4) - for your records and tax.

    .venv\\Scripts\\python.exe tools\\export_trades.py --from 2026-10-01 --to 2026-10-31
    .venv\\Scripts\\python.exe tools\\export_trades.py --what paper --out C:\\exports

Dates are London dates, inclusive, on the ENTRY time. Writes manual_trades_<from>_<to>.csv
and/or paper_trades_<from>_<to>.csv. Times are exported in both UTC and London.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select  # noqa: E402

from core.config import load_config  # noqa: E402
from data.db import Database  # noqa: E402
from data.trade_db import ManualTradeRow, PaperTradeRow  # noqa: E402

MANUAL_COLS = ["trade_ref", "signal_id", "source", "symbol", "status", "entry_price", "entry_order", "margin",
               "leverage", "notional", "qty", "initial_stop", "current_stop", "tp1", "tp2", "liq_price", "risk_usd",
               "net_pnl", "funding_usd", "r_multiple", "plan_pnl", "entry_gap_pct", "notes"]
PAPER_COLS = ["signal_id", "policy", "symbol", "setup", "score", "regime", "session", "suppressed_reason", "status",
              "entry_ref", "entry_fill", "qty", "notional", "initial_stop", "tp1", "tp2", "risk_usd", "exit_reason",
              "net_pnl", "funding_usd", "r_multiple", "mfe_pct", "mae_pct"]


def _times(ms, tz):
    if ms is None:
        return "", ""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S"), dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def export(cfg, start: str, end: str, what: str, out_dir: Path) -> list[Path]:
    tz = ZoneInfo(cfg.app.display_tz)
    lo = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=tz)
    hi = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=1)
    lo_ms, hi_ms = int(lo.timestamp() * 1000), int(hi.timestamp() * 1000)
    db = Database(cfg.database.url.format(data_dir=cfg.data_dir.as_posix()))
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    jobs = []
    if what in ("manual", "both"):
        jobs.append(("manual_trades", ManualTradeRow, MANUAL_COLS, "closed_ms"))
    if what in ("paper", "both"):
        jobs.append(("paper_trades", PaperTradeRow, PAPER_COLS, "exit_ms"))
    for name, model, cols, exit_col in jobs:
        path = out_dir / f"{name}_{start}_{end}.csv"
        with db.session() as s, open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["entry_time_utc", "entry_time_london", "exit_time_utc", "exit_time_london", *cols, "legs"])
            q = select(model).where(model.entry_ms >= lo_ms, model.entry_ms < hi_ms).order_by(model.entry_ms)
            n = 0
            for r in s.execute(q).scalars():
                legs = "; ".join(f"{_times(l['ts'], tz)[1]} {l.get('kind', '')} "
                                 f"{l.get('qty', l.get('fraction', '')):.6g} @ {l['price']:g}" for l in (r.legs or []))
                w.writerow([*_times(r.entry_ms, tz), *_times(getattr(r, exit_col), tz),
                            *[getattr(r, c) if getattr(r, c) is not None else "" for c in cols], legs])
                n += 1
        print(f"{n} rows -> {path}")
        written.append(path)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description="Export trades to CSV")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ap.add_argument("--from", dest="start", default="2000-01-01", help="first London date (YYYY-MM-DD)")
    ap.add_argument("--to", dest="end", default=today, help="last London date (YYYY-MM-DD), inclusive")
    ap.add_argument("--what", choices=("manual", "paper", "both"), default="both")
    ap.add_argument("--out", default=None, help="output folder (default: <data_dir>/exports)")
    a = ap.parse_args()
    cfg = load_config()
    export(cfg, a.start, a.end, a.what, Path(a.out) if a.out else cfg.data_dir / "exports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
