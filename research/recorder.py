"""Live research recorder: after every 5m scan, one row per universe coin (research.schema)
appended to <data_dir>/research/snapshots/YYYY-MM-DD.csv.gz (UTC days).

Captures what no backtest can reconstruct later: open interest, the live funding forecast,
mark-vs-index premium, order-book spread and depth, plus exactly what the scanner decided.
About 41 coins x 288 bars = ~12k rows a day, roughly 1-2 MB compressed.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
from datetime import datetime, timezone
from pathlib import Path

from research.schema import COLUMNS, build_row, fmt

log = logging.getLogger("research")


class Recorder:
    def __init__(self, eng):
        self.eng = eng
        self.dir = Path(eng.cfg.data_dir) / "research" / "snapshots"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rows_written = 0

    def _live(self, sym: str, now: int) -> dict:
        eng = self.eng
        p = eng.premium.data.get(sym)
        cand = next((c for c in eng.universe.candidates if c.symbol == sym), None)
        oi = eng.oi._oi.get(sym)
        norm = eng.cfg.funding.normalise_to_min
        t_last = eng.tape.last_trade_ms.get(sym)
        return {
            "premium_pct": (p.mark / p.index - 1) * 100 if p and p.index else None,
            "funding_fc_8h": p.forecast_funding * norm / p.cycle_min * 100 if p and p.cycle_min else None,
            "funding_last_8h": p.last_funding * norm / p.cycle_min * 100 if p and p.cycle_min else None,
            "oi_raw": oi[-1] if oi else None,
            "spread_pct": cand.spread_pct if cand else None, "depth_usd": cand.bid_depth_usd if cand else None,
            "qv24h": cand.quote_volume_24h if cand else None, "listing_age_d": cand.listing_age_days if cand else None,
            "tape_age_s": (now - t_last) / 1000 if t_last else None,
        }

    def rows(self, as_of: int, signals: list) -> list[dict]:
        eng = self.eng
        now = eng.clock.now_ms()
        by_sym = {s.symbol: s.to_record() for s in signals}
        out = []
        for sym, f in eng.features.items():
            b5 = eng.store.series(sym, "5m").arrays().upto(as_of)
            try:
                out.append(build_row(as_of, f, eng.scan_rows.get(sym), b5, eng.regime, eng.cfg.hash,
                                     self._live(sym, now), by_sym.get(sym)))
            except Exception:  # noqa: BLE001 - research data must never disturb the scanner
                log.exception("research row failed for %s", sym)
        return out

    def write(self, as_of: int, rows: list[dict]) -> None:
        if not rows:
            return
        day = datetime.fromtimestamp(as_of / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        p = self.dir / f"{day}.csv.gz"
        buf = io.StringIO()
        w = csv.writer(buf)
        if not p.exists():
            w.writerow(COLUMNS)
        for r in rows:
            w.writerow([fmt(r.get(c)) for c in COLUMNS])
        with gzip.open(p, "at", encoding="utf-8", newline="") as fh:   # each append = one gzip member
            fh.write(buf.getvalue())
        self.rows_written += len(rows)

    def files(self) -> list[dict]:
        return [{"name": p.name, "bytes": p.stat().st_size} for p in sorted(self.dir.glob("*.csv.gz"))]


async def nightly_loop(eng) -> None:
    """Every night at research.mine_daily_at (London): mine the recorded days -> reports/live.json."""
    import asyncio
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from research import mine
    r = eng.cfg.research
    tz = ZoneInfo(eng.cfg.app.display_tz)
    while True:
        now = datetime.now(tz)
        hh, mm = (int(x) for x in str(r.mine_daily_at).split(":"))
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            days = len(list((Path(eng.cfg.data_dir) / "research" / "snapshots").glob("*.csv.gz")))
            if days >= int(r.min_days_to_mine):
                def job():
                    rep = mine.run(mine.load_live(eng.cfg), eng.cfg, "live")
                    mine.save(eng.cfg, rep, "live")
                await asyncio.to_thread(job)
                log.info("research: nightly live report written (%d days)", days)
        except Exception:  # noqa: BLE001
            log.exception("research: nightly mining failed")
