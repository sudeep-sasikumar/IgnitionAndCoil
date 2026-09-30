"""Historical research dataset: the same rows the live recorder writes, rebuilt from cached WEEX
history through the live code (features, regime, levels, signal engine with its cooldown), plus
outcome labels and - for breakout bars - the exact simulated Policy S / L trade.

    .venv\\Scripts\\python.exe -m research.history --tag y2026 --days 365 --end "2026-09-30 01:20" --symbols ...

Rows kept: every bar where Ignition's trigger holds (close above the 12h high) + every coin on
every full hour (a regular sample for "what came before big moves"). Live-only columns (order
book, premium, raw OI) are empty; OI changes are empty where the app has no snapshots.
Writes <data_dir>/research/history/<tag>.csv.gz.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import gzip
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.__main__ import _say, prepare  # noqa: E402
from backtest.engine import Backtester, regime_block  # noqa: E402
from core.clock import Clock, parse_local  # noqa: E402
from core.config import Config, apply_overrides, load_config  # noqa: E402
from core.engine import default_policy_missing  # noqa: E402
from core.logs import setup_logging  # noqa: E402
from data.db import Database  # noqa: E402
from exchange.weex_rest import WeexRest  # noqa: E402
from features.compute import compute_features  # noqa: E402
from levels.resistance import headroom  # noqa: E402
from research.labels import label_arrays  # noqa: E402
from research.schema import COLUMNS, LABELS, build_row, fmt  # noqa: E402
from signals.engine import SignalEngine  # noqa: E402
from signals.regime import compute_regime  # noqa: E402
from signals.setups import eval_ignition  # noqa: E402

M5, H1, DAY = 300_000, 3_600_000, 86_400_000
EXTRA = ["sample", "trade_r", "trade_net", "trade_exit", "trade_r_l"]


class HistoryBuilder(Backtester):
    def build(self, start_ms: int, end_ms: int, out: Path, progress=None) -> int:
        cfg = self.cfg
        r = cfg.research
        se = SignalEngine(cfg)
        dis = se.disabled
        refresh = cfg.backtest.universe_refresh_min * 60_000
        labels = {}
        for sym, tfs in self.inp.bars.items():
            b = tfs["5m"]
            if len(b):
                lab = label_arrays(b.t, b.h, b.l, b.c, float(r.tp_pct), float(r.sl_pct))
                labels[sym] = (b.tc, lab)
        universe: list[str] = []
        sent_times: list[int] = []
        n = 0
        T = start_ms - start_ms % M5 + M5
        day = None
        with gzip.open(out, "wt", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(COLUMNS + LABELS + EXTRA)
            while T <= end_ms:
                if not universe or T % refresh == 0:
                    universe = self.universe_at(T)
                btc5, btc15, btc1h = (self.arrays(self.btc, tf, T) for tf in ("5m", "15m", "1h"))
                regime = None
                if len(btc5) and len(btc15) and len(btc1h) and int(btc5.tc[-1]) == T:
                    br, nb = self.breadth(universe, T)
                    regime = compute_regime(btc5, btc15, btc1h, br, nb, cfg)
                bar_rows = []
                for sym in universe:
                    b5 = self.arrays(sym, "5m", T)
                    if not len(b5) or int(b5.tc[-1]) != T:
                        continue
                    b15, b1h, b4h = (self.arrays(sym, tf, T) for tf in ("15m", "1h", "4h"))
                    if len(b15) < 5 or len(b1h) < 5:
                        continue
                    f = compute_features(sym, b5, b15, b1h, btc5, self.market_inputs(sym, T), cfg, memo=self.memo)
                    prec, brk = self.inp.precision.get(sym), self.inp.brackets.get(sym)
                    row, sigs, _w = se.evaluate(sym, T, f, b5, b15, b1h, b4h, regime, brk, f.price, prec)
                    brk_ok = any(c["key"] == "ignition.break" and c["passed"] for c in row.ignition_conds) and f.warm
                    if not brk_ok and T % H1 != 0 and not sigs:
                        continue
                    trade = {}
                    if brk_ok:          # the exact trade this breakout would have been (whatever the filters)
                        lm = se.levels(sym, b1h, b4h)
                        hr = headroom(lm, f.price, f.atr_1h_pct, cfg)
                        ev = eval_ignition(f, b5, hr, cfg, dis)
                        sig = se._make_signal(ev, sym, T, f, row.score, row.breakdown, regime, lm, f.price, brk, prec)
                        rec = sig.to_record()
                        rec["signal_id"], rec["suppressed_reason"] = "H", None
                        for t in self.simulate(rec, end_ms):
                            if t["status"] == "CLOSED":
                                if t["policy"] == "S":
                                    trade.update(trade_r=t["r"], trade_net=t["net"], trade_exit=t["exit_reason"])
                                else:
                                    trade["trade_r_l"] = t["r"]
                    sig_rec = sigs[0].to_record() if sigs else None
                    bar_rows.append((sym, f, row, b5, sig_rec, "break" if brk_ok else "hourly", trade))
                # live gating for the signals of this bar (hourly cap etc.), highest score first
                for sym, f, row, b5, sig_rec, sample, trade in sorted(bar_rows, key=lambda x: -(x[4] or {}).get("score", -1)):
                    if sig_rec:
                        sig_rec["suppressed_reason"] = self._gate(sig_rec, regime, sent_times, T)
                for sym, f, row, b5, sig_rec, sample, trade in bar_rows:
                    d = build_row(T, f, row, b5, regime, cfg.hash, None, sig_rec)
                    tc, lab = labels[sym]
                    i = int(tc.searchsorted(T))
                    for k in LABELS:
                        d[k] = float(lab[k][i]) if i < len(tc) and tc[i] == T else math.nan
                    d.update(sample=sample, trade_r=trade.get("trade_r", math.nan), trade_net=trade.get("trade_net", math.nan),
                             trade_exit=trade.get("trade_exit", ""), trade_r_l=trade.get("trade_r_l", math.nan))
                    w.writerow([fmt(d.get(c)) for c in COLUMNS + LABELS + EXTRA])
                    n += 1
                d_ = T // DAY
                if progress and d_ != day:
                    day = d_
                    progress(T, n, len(universe))
                T += M5
        return n

    def _gate(self, sig: dict, regime, sent_times: list[int], T: int) -> str | None:
        cfg = self.cfg
        reason = regime_block(SimpleNamespace(side=sig.get("side", "LONG"), tags=sig.get("tags", [])), regime, cfg)
        if reason is None:
            recent = [t for t in sent_times if t > T - H1]
            if any(t.startswith("BLACKOUT") for t in sig.get("tags", [])) and not cfg.signals.blackout_alerts:
                reason = "blackout"
            elif default_policy_missing(sig, cfg):
                reason = "default_policy_na"
            elif len(recent) >= cfg.signals.max_alerts_per_hour:
                reason = "hourly_cap"
            else:
                sent_times.append(T)
        return reason


async def main_async(a) -> int:
    base = load_config(a.config)
    d = copy.deepcopy(base.to_dict())
    apply_overrides(d, a.set or [])
    d["exchange"]["rate_limit_weight"] = d["backtest"]["rate_limit_weight"]
    cfg = Config(d, base.path)
    setup_logging(cfg.data_dir / "logs", cfg.app.log_level)
    clock = Clock(cfg.app.display_tz)
    rest = WeexRest(cfg, clock)
    db = Database(cfg.database.url.format(data_dir=cfg.data_dir.as_posix()))
    try:
        prep = await prepare(cfg, rest, db, clock, a.days, a.symbols, None, a.set or [],
                             end_ms=parse_local(a.end, cfg.app.display_tz) if a.end else None)
    finally:
        await rest.close()
    out_dir = Path(cfg.data_dir) / "research" / "history"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{a.tag}.csv.gz"
    t0 = time.time()
    hb = HistoryBuilder(cfg, prep["inp"], prep["disabled"])
    n = hb.build(prep["start"], prep["end"], out,
                 lambda T, n_, u: _say(f"  {clock.fmt(T, with_date=True)[:10]}: {n_:,} rows, universe {u}"))
    _say(f"{n:,} rows in {time.time() - t0:.0f}s -> {out}")
    return 0


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Build the historical research dataset")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--end", default=None)
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--set", action="append", metavar="KEY=VALUE")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
