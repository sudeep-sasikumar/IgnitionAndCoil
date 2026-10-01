"""Hindsight-free backtest over the pool from research.pool, downloading only what matters.

    .venv\\Scripts\\python.exe -m research.sparse --period y2026 [--only-40] [--tag ...]

A coin can only produce a signal while it is in the universe, i.e. on its "active" days (two
consecutive UTC days with >= universe.min_quote_volume_24h traded - research.pool). So each coin
is downloaded only around its active days, with exactly the warm-up the live ring buffers hold
(bars.keep: 5m 1000 bars ~3.5 d, 15m 400 ~4.2 d (built from 5m), 1h 1000 ~41.7 d, 4h 400 ~66.7 d)
plus 2 days after (trade exits). Every indicator therefore sees the same bars as a full download:
the backtester skips a coin at any bar it has no data for, and outside active days a coin cannot
pass the volume filter anyway. --only-40 replays the old "today's top 40" pool, which must
reproduce the full-data backtest exactly (the proof that the shortcut changes nothing).
Downloads are cached in <data_dir>/research/sparse/ (existing full downloads are reused).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.__main__ import _say  # noqa: E402
from backtest.data import FIELDS, _atomic_write, merge_arrays  # noqa: E402
from backtest.engine import Backtester, BTInputs  # noqa: E402
from backtest.report import build  # noqa: E402
from core.clock import TF_MS, Clock, floor_tf, parse_local  # noqa: E402
from core.config import Config, load_config  # noqa: E402
from core.logs import setup_logging  # noqa: E402
from data.bars import BarArrays  # noqa: E402
from data.db import Database  # noqa: E402
from exchange.weex_rest import WeexRest  # noqa: E402
from plan.liquidation import parse_risk_limits  # noqa: E402

DAY = 86_400_000
WARM = {"5m": 5 * DAY, "1h": 43 * DAY, "4h": 68 * DAY}      # >= bars.keep of each timeframe
AFTER = 2 * DAY


def active_days(t: np.ndarray, qv: np.ndarray, start: int, end: int, thr: float) -> np.ndarray:
    """UTC day starts on which the coin could have been in the universe."""
    m = (t >= start - DAY) & (t <= end)
    tt, qq = t[m], qv[m]
    act = np.zeros(len(qq), bool)
    two = qq[:-1] + qq[1:] >= thr
    act[:-1] |= two
    act[1:] |= two
    return tt[act]


def intervals(days: np.ndarray, warm: int, after: int, lo: int, hi: int) -> list[tuple[int, int]]:
    """Merged [a, b) ranges covering each active day with warm-up before and `after` after."""
    out: list[list[int]] = []
    for d in sorted(days):
        a, b = max(lo, int(d) - warm), min(hi, int(d) + DAY + after)
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out if b > a]


class SparseCache:
    """Per coin and timeframe: bars plus the list of time ranges already downloaded."""

    def __init__(self, cfg, rest):
        self.cfg, self.rest = cfg, rest
        self.dir = Path(cfg.data_dir) / "research" / "sparse"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.full = Path(cfg.data_dir) / cfg.backtest.cache_dir

    def _load(self, sym: str, tf: str) -> tuple[BarArrays | None, list[tuple[int, int]]]:
        bars, segs = None, []
        p = self.dir / f"{sym}_{tf}.npz"
        if p.exists():
            with np.load(p) as z:
                bars = BarArrays(**{f: z[f] for f in FIELDS})
                segs = [tuple(map(int, x)) for x in z["segs"]]
        f = self.full / f"{sym}_{tf}.npz"                        # a full backtest download, if any
        if f.exists():
            with np.load(f) as z:
                bars = merge_arrays(bars, BarArrays(**{k: z[k] for k in FIELDS}))
                segs.append((int(z["req_from"]), int(z["req_to"]) + TF_MS[tf]))
        return bars, segs

    @staticmethod
    def _missing(want: tuple[int, int], segs: list[tuple[int, int]]) -> list[tuple[int, int]]:
        gaps, (a, b) = [], want
        for s, e in sorted(segs):
            if e <= a or s >= b:
                continue
            if s > a:
                gaps.append((a, s))
            a = max(a, e)
        if a < b:
            gaps.append((a, b))
        return gaps

    async def bars(self, sym: str, tf: str, ranges: list[tuple[int, int]], now_ms: int) -> BarArrays:
        have, segs = self._load(sym, tf)
        step = TF_MS[tf]
        fetched = False
        for want in ranges:
            for a, b in self._missing(want, segs):
                a = floor_tf(a, tf)
                b = min(floor_tf(b, tf), floor_tf(now_ms, tf) - step)
                if b < a:
                    continue
                got = BarArrays.from_bars(await self.rest.history_klines(sym, tf, a, b))
                have = merge_arrays(have, got)
                segs.append((a, b + step))
                fetched = True
        if have is None:
            have = BarArrays.from_bars([])
        if fetched:
            h = have
            _atomic_write(self.dir / f"{sym}_{tf}.npz", lambda fh: np.savez(
                fh, segs=np.array(segs, np.int64).reshape(-1, 2), **{f: getattr(h, f) for f in FIELDS}))
        keep = np.zeros(len(have), bool)                          # only the requested ranges
        for a, b in ranges:
            keep |= (have.t >= a) & (have.t < b)
        return BarArrays(**{f: getattr(have, f)[keep] for f in FIELDS})

    async def funding(self, sym: str, ranges: list[tuple[int, int]]) -> list[tuple[int, float, float]]:
        p = self.dir / f"{sym}_funding.json"
        data = json.loads(p.read_text()) if p.exists() else {"segs": [], "rows": []}
        rows = {int(r[0]): r for r in data["rows"]}
        segs = [tuple(x) for x in data["segs"]]
        f = self.full / f"{sym}_funding.json"                     # a full backtest download, if any
        if f.exists():                                            # (WEEX only keeps ~364 days: reuse it)
            full = json.loads(f.read_text())
            rows.update({int(r[0]): r for r in full.get("rows", [])})
            if full.get("rows"):
                segs.append((int(full["from"]), int(full["to"])))
        fetched = False
        oldest = int(time.time() * 1000) - 364 * DAY              # WEEX: "within the last 365 days"
        for want in ranges:
            for a, b in self._missing(want, segs):
                if b <= oldest:                                   # too old: nothing to fetch, ever
                    segs.append((a, b))
                    fetched = True
                    continue
                for r in await self.rest.funding_history(sym, max(a, oldest), b):
                    rows[int(r["fundingTime"])] = [int(r["fundingTime"]), float(r["fundingRate"]),
                                                   float(r.get("markPrice") or 0)]
                segs.append((a, b))
                fetched = True
        if fetched:
            out = {"segs": segs, "rows": sorted(rows.values())}
            _atomic_write(p, lambda fh: fh.write(json.dumps(out).encode()))
        return [(int(t), float(r), float(m)) for t, r, m in sorted(rows.values())]


def complete_15m(b5: BarArrays) -> BarArrays:
    """15m bars from 5m, only where all three 5m bars exist (gaps between windows)."""
    if not len(b5):
        return b5
    key = b5.t - b5.t % TF_MS["15m"]
    starts = np.r_[0, np.flatnonzero(np.diff(key)) + 1]
    ends = np.r_[starts[1:], len(key)]
    full = (ends - starts) == 3
    s, e = starts[full], ends[full]
    red = lambda arr, fn: fn.reduceat(arr, starts)[full]        # noqa: E731
    return BarArrays(t=key[s], o=b5.o[s], h=red(b5.h, np.maximum), l=red(b5.l, np.minimum), c=b5.c[e - 1],
                     v=red(b5.v, np.add), qv=red(b5.qv, np.add), tbv=red(b5.tbv, np.add),
                     tbqv=red(b5.tbqv, np.add), tc=key[s] + TF_MS["15m"])


async def prepare_sparse(cfg, rest, db, clock, pool: list[str], vols: dict, start: int, end: int) -> tuple[BTInputs, dict]:
    server, b, af = await rest.server_time()
    clock.set_offset(server, b, af)
    now = clock.now_ms()
    info = await rest.exchange_info()
    meta = {s["symbol"]: s for s in info["symbols"]}
    cache = SparseCache(cfg, rest)
    inp = BTInputs(bars={}, funding={})
    inp.brackets = parse_risk_limits(await rest.risk_limits_all())
    thr = float(cfg.universe.min_quote_volume_24h)
    btc = cfg.universe.regime_reference
    syms = [btc] + [s for s in pool if s != btc]
    sem = asyncio.Semaphore(int(cfg.backtest.download_concurrency))
    t0, done = time.time(), 0
    stats = {"coins": len(syms) - 1, "active_days": 0, "bars_5m": 0}

    async def load(sym: str) -> None:
        nonlocal done
        async with sem:
            if sym == btc:                                       # regime needs BTC everywhere
                days = np.arange(start - DAY - start % DAY, end + DAY, DAY)
            else:
                t, q = vols[sym]
                days = active_days(t, q, start, end, thr)
                stats["active_days"] += len(days)
            if not len(days):
                done += 1
                return
            bars = {}
            r5 = intervals(days, WARM["5m"], AFTER, start - WARM["5m"], end + TF_MS["5m"])
            bars["5m"] = await cache.bars(sym, "5m", r5, now)
            bars["15m"] = complete_15m(bars["5m"])
            for tf in ("1h", "4h"):
                bars[tf] = await cache.bars(sym, tf, intervals(days, WARM[tf], AFTER, start - WARM[tf],
                                                               end + TF_MS[tf]), now)
            inp.bars[sym] = bars
            inp.funding[sym] = await cache.funding(sym, intervals(days, DAY, AFTER, start - DAY, end))
            m = meta.get(sym) or {}
            inp.precision[sym] = int(m["pricePrecision"]) if m.get("pricePrecision") is not None else None
            first = db.first_bar_ts(sym)
            if first is None and sym in vols:
                first = int(vols[sym][0][0])
                if len(vols[sym][0]) >= 30:
                    db.set_first_bar_ts(sym, first)
            if first is not None:
                inp.first_bar[sym] = first
            stats["bars_5m"] += len(bars["5m"])
            done += 1
            el = time.time() - t0
            if done % 10 == 0 or done == len(syms):
                _say(f"  [{done}/{len(syms)}] {sym}: {len(bars['5m']):,} 5m bars (elapsed {el:.0f}s, "
                     f"ETA {el / done * (len(syms) - done):.0f}s)")
    await asyncio.gather(*(load(s) for s in syms))
    inp.bars = {s: inp.bars[s] for s in syms if s in inp.bars}
    return inp, stats


async def main_async(a) -> int:
    base = load_config(a.config)
    d = base.to_dict()
    d["exchange"]["rate_limit_weight"] = int(a.rate)
    d["backtest"]["download_concurrency"] = int(a.concurrency)
    cfg = Config(d, base.path)
    setup_logging(cfg.data_dir / "logs", cfg.app.log_level)
    pool_info = json.loads((Path(cfg.data_dir) / "research" / "pool.json").read_text())
    per = pool_info["periods"][a.period]
    end = floor_tf(parse_local(per["end"], cfg.app.display_tz), "5m")
    start = end - 365 * DAY
    z = np.load(Path(cfg.data_dir) / "research" / "daily_vol.npz")
    vols = {k[:-3]: (z[k], z[k[:-3] + "__q"]) for k in z.files if k.endswith("__t")}
    pool = per["symbols"]
    if a.only_40:
        pool = [s for s in (Path(cfg.data_dir).parent / "var" / "syms_365_pool.txt").read_text().strip().split(",")]
    clock = Clock(cfg.app.display_tz)
    rest = WeexRest(cfg, clock)
    db = Database(cfg.database.url.format(data_dir=cfg.data_dir.as_posix()))
    t0 = time.time()
    try:
        _say(f"{a.period}: {len(pool)} coins - downloading only around their active days...")
        inp, st = await prepare_sparse(cfg, rest, db, clock, pool, vols, start, end)
    finally:
        await rest.close()
    _say(f"  {st['active_days']:,} active coin-days, {st['bars_5m']:,} 5m bars in memory ({time.time() - t0:.0f}s)")
    disabled = {"oi"}                                          # no OI history
    fcov = [len([r for r in inp.funding.get(s, []) if start <= r[0] <= end]) for s in list(inp.bars)[1:]]
    if not any(fcov) or np.mean([x > 0 for x in fcov]) < 0.8:
        disabled.add("funding")                                # WEEX serves ~1 year of funding
    _say(f"Replaying (disabled groups: {sorted(disabled)})...")
    res = Backtester(cfg, inp, disabled).run(start, end, lambda T, n, u: _say(
        f"  {clock.fmt(T, with_date=True)[:10]}: {n} signals, universe {u}") if a.verbose else None)
    out_dir = cfg.data_dir / cfg.backtest.report_dir
    tag = a.tag or (f"{a.period}_top40" if a.only_40 else f"{a.period}_unbiased")
    import csv
    cols = ["signal_id", "policy", "symbol", "setup", "score", "regime", "suppressed_reason", "status", "entry_ms",
            "exit_ms", "exit_reason", "net", "r"]
    with open(out_dir / f"sparse_{tag}_trades.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for t in res["trades"]:
            w.writerow([t.get(c, "") if t.get(c) is not None else "" for c in cols])
    meta = {"symbols": list(inp.bars), "config_hash": cfg.hash, "generated_ms": int(time.time() * 1000), "runtime_s": time.time() - t0,
            "notes": [f"Hindsight-free pool: {len(pool)} coins that could have qualified during the period "
                      f"(research.pool), downloaded around their active days only (research.sparse)."]}
    (out_dir / f"sparse_{tag}.html").write_text(build(res, meta, cfg), encoding="utf-8")
    sent = [t for t in res["trades"] if t["policy"] == "S" and t["status"] == "CLOSED" and not t.get("suppressed_reason")]
    r = np.array([t["r"] for t in sent])
    net = sum(t["net"] for t in sent)
    syms_traded = len({t["symbol"] for t in sent})
    _say(f"RESULT {tag}: {len(sent)} sent trades on {syms_traded} coins, sum R {r.sum():+.4f}, avg {r.mean() if len(r) else 0:+.3f}R, "
         f"net ${net:+,.2f}  ({time.time() - t0:.0f}s)")
    return 0


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Hindsight-free backtest (downloads only active windows)")
    ap.add_argument("--period", required=True, help="tag in research/pool.json, e.g. y2026")
    ap.add_argument("--only-40", action="store_true", help="the old top-40 pool (must match the full backtest)")
    ap.add_argument("--tag", default="")
    ap.add_argument("--rate", type=int, default=450)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--config", default=None)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
