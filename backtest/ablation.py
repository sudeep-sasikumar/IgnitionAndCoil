"""Filter ablation: which Ignition / Coil conditions earn their keep?

    .venv\\Scripts\\python.exe -m backtest.ablation harvest --tag y2026 --end "2026-09-30 01:20" --days 365 --symbols ...
    .venv\\Scripts\\python.exe -m backtest.ablation search --tags y2025,y2026

1. harvest - ONE replay of the period through the live code (features, regime, levels, setups,
   score, trade plan, exit engine). Every bar where Ignition's own trigger holds (close above
   the 12h high, features warm) is kept with the pass/fail of each other condition, its score
   and its simulated Policy S / L trade. For Coil, every bar's WATCH conditions + coil box and
   every 15m close's ENTRY conditions are kept; Coil trades are simulated on demand (their stop
   depends on which box is active).
2. search - every on/off combination of the removable conditions (and a few score cut-offs) is
   replayed from that record with the live rules re-applied exactly: per-symbol cooldown (set
   even when an alert is suppressed), Ignition before Coil on the same bar, Coil WATCH expiry and
   consumption, the hourly alert cap and "Policy S must fit". The all-conditions combination
   must reproduce the normal backtest exactly - that is checked, not assumed.
Results are written as JSON + CSV next to the backtest reports.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import copy
import csv
import itertools
import json
import math
import pickle
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.__main__ import _say, prepare  # noqa: E402
from backtest.engine import Backtester  # noqa: E402
from core.clock import Clock, parse_local  # noqa: E402
from core.config import Config, apply_overrides, load_config  # noqa: E402
from core.engine import default_policy_missing  # noqa: E402
from core.logs import setup_logging  # noqa: E402
from data.db import Database  # noqa: E402
from exchange.weex_rest import WeexRest  # noqa: E402
from features.compute import compute_features  # noqa: E402
from levels.resistance import headroom  # noqa: E402
from plan.trade_plan import build_plan  # noqa: E402
from signals.engine import SignalEngine  # noqa: E402
from signals.regime import compute_regime  # noqa: E402
from signals.score import compute_score  # noqa: E402
from signals.setups import COIL, IGNITION, CoilTracker, CoilWatch, eval_ignition, off  # noqa: E402

M5, H1, DAY = 300_000, 3_600_000, 86_400_000
# removable conditions ("ignition.break" / "coil.watch.bbw" define the setups; OI has no history)
IGN_KEYS = ["ignition.rvol_5m", "ignition.rvol_15m", "ignition.vwap", "ignition.ema", "ignition.ret_1h",
            "ignition.ret_24h", "ignition.rs_1h", "ignition.taker", "ignition.cvd", "ignition.funding",
            "ignition.headroom", "ignition.close_pos", "ignition.candle_size"]
WATCH_KEYS = ["coil.watch.trend", "coil.watch.ema_slope", "coil.watch.ret_4h", "coil.watch.funding"]
ENTRY_KEYS = ["coil.rvol_15m", "coil.taker", "coil.headroom"]
SCORES = [0, 40, 55, 70]


def _passes(conds, key: str, disabled: set[str]) -> bool:
    c = next(c for c in conds if c.key == key)
    return bool(c.passed) or off(c, disabled)


def _bits(conds, keys: list[str], disabled: set[str]) -> int:
    return sum(1 << i for i, k in enumerate(keys) if _passes(conds, k, disabled))


def _trade_row(trades: list[dict], policy: str) -> tuple:
    t = next((x for x in trades if x["policy"] == policy), None)
    if t is None:
        return (False, math.nan, math.nan, 0)
    closed = t["status"] == "CLOSED"
    return (True, t["r"] if closed else math.nan, t["net"] if closed else math.nan, int(t.get("exit_ms") or 0))


# ---- 1. harvest ------------------------------------------------------------------------------------

class Harvester(Backtester):
    def harvest(self, start_ms: int, end_ms: int, progress=None) -> dict:
        """Same loop as Backtester.run, recording candidates instead of firing signals."""
        cfg = self.cfg
        se = SignalEngine(cfg)
        dis = se.disabled
        refresh = cfg.backtest.universe_refresh_min * 60_000
        box_n = int(cfg.coil.box_bars_1h)
        probe = CoilTracker(cfg)
        ign: list[tuple] = []
        watch = defaultdict(list)     # sym -> (T, base_ok, bits, box_high)
        entry = defaultdict(list)     # sym -> (T, bits, c15, atr15, ref, resistance, score, regime)
        regime_at: dict[int, str] = {}
        universe: list[str] = []
        T = start_ms - start_ms % M5 + M5
        day = None
        while T <= end_ms:
            if not universe or T % refresh == 0:
                universe = self.universe_at(T)
            btc5, btc15, btc1h = (self.arrays(self.btc, tf, T) for tf in ("5m", "15m", "1h"))
            regime = None
            if len(btc5) and len(btc15) and len(btc1h) and int(btc5.tc[-1]) == T:
                br, n = self.breadth(universe, T)
                regime = compute_regime(btc5, btc15, btc1h, br, n, cfg)
            rstate = regime.state if regime else "NEUTRAL"
            regime_at[T] = rstate
            for sym in universe:
                b5 = self.arrays(sym, "5m", T)
                if not len(b5) or int(b5.tc[-1]) != T:
                    continue
                b15, b1h, b4h = (self.arrays(sym, tf, T) for tf in ("15m", "1h", "4h"))
                if len(b15) < 5 or len(b1h) < 5:
                    continue
                f = compute_features(sym, b5, b15, b1h, btc5, self.market_inputs(sym, T), cfg, memo=self.memo)
                lm = se.levels(sym, b1h, b4h)
                hr = headroom(lm, f.price, f.atr_1h_pct, cfg)
                prec, brk = self.inp.precision.get(sym), self.inp.brackets.get(sym)
                # Ignition: only bars where the setup's own trigger holds
                ev = eval_ignition(f, b5, hr, cfg, dis)
                if f.warm and _passes(ev.conds, "ignition.break", dis):
                    score, bd = compute_score(IGNITION, f, rstate, hr, cfg, dis)
                    sig = se._make_signal(ev, sym, T, f, score, bd, regime, lm, f.price, brk, prec)
                    rec = sig.to_record()
                    rec["signal_id"], rec["suppressed_reason"] = f"H-{len(ign)}", None
                    trades = self.simulate(rec, end_ms)
                    ign.append((sym, T, _bits(ev.conds, IGN_KEYS, dis), score, rstate,
                                rec["plan"]["stop_s"]["price"] is not None, rec["plan"]["stop_l"]["price"] is not None,
                                *_trade_row(trades, "S"), *_trade_row(trades, "L")))
                # Coil WATCH conditions on every bar (the box is re-measured whenever they hold)
                wc, _ = probe.watch_conditions(f, cfg, dis)
                base = f.warm and len(b1h) >= box_n and _passes(wc, "coil.watch.bbw", dis) and _passes(wc, "coil.watch.oi", dis)
                box = float(b1h.h[-box_n:].max()) if len(b1h) >= box_n else math.nan
                watch[sym].append((T, base, _bits(wc, WATCH_KEYS, dis), box))
                # Coil ENTRY conditions at 15m closes (the break test needs the combination's box)
                if T % 900_000 == 0 and len(b15):
                    probe.watches[sym] = CoilWatch(sym, T, T, -math.inf, -math.inf)
                    ce = probe.eval_entry(sym, f, b15, hr, dis)
                    probe.watches.pop(sym, None)
                    score, _ = compute_score(COIL, f, rstate, hr, cfg, dis)
                    entry[sym].append((T, _bits(ce.conds, ENTRY_KEYS, dis), float(b15.c[-1]), f.atr_15m, f.price,
                                       hr.level.price if hr.level else None, score, rstate))
            d = T // DAY
            if progress and d != day:
                day = d
                progress(T, len(ign), len(universe))
            T += M5
        return {"ign": ign, "watch": dict(watch), "entry": dict(entry), "regime_at": regime_at,
                "start": start_ms, "end": end_ms, "disabled": sorted(dis)}

    def coil_trade(self, sym: str, T: int, ref: float, stop: float | None, res: float | None, score: int,
                   rstate: str, end_ms: int) -> tuple:
        plan = build_plan(sym, COIL, ref, stop, res, self.inp.brackets.get(sym), self.cfg,
                          precision=self.inp.precision.get(sym)).as_dict()
        rec = {"signal_id": f"C-{sym}-{T}", "symbol": sym, "setup": COIL, "bar_close_ms": T, "score": score,
               "regime": {"state": rstate}, "session": "", "suppressed_reason": None, "tags": [], "plan": plan}
        trades = self.simulate(rec, end_ms)
        return (plan["stop_s"]["price"] is not None, plan["stop_l"]["price"] is not None,
                *_trade_row(trades, "S"), *_trade_row(trades, "L"))


# ---- 2. search ------------------------------------------------------------------------------------

class Year:
    """One harvested period, arranged for fast combination replays."""

    def __init__(self, tag: str, h: dict, bt: Harvester | None):
        self.tag, self.bt, self.end = tag, bt, h["end"]
        ign = sorted(h["ign"], key=lambda r: (r[0], r[1]))
        self.sym = np.array([r[0] for r in ign])
        self.T = np.array([r[1] for r in ign], dtype=np.int64)
        self.bits = np.array([r[2] for r in ign], dtype=np.int64)
        self.score = np.array([r[3] for r in ign], dtype=np.int64)
        self.s_ok = np.array([r[5] for r in ign], dtype=bool)
        self.rS = np.array([r[8] for r in ign], dtype=float)
        self.netS = np.array([r[9] for r in ign], dtype=float)
        self.exitS = np.array([r[10] for r in ign], dtype=np.int64)
        self.seg = {}
        for s in np.unique(self.sym):
            idx = np.flatnonzero(self.sym == s)
            self.seg[s] = (int(idx[0]), int(idx[-1]) + 1)
        self.watch = {s: (np.array([w[0] for w in v], dtype=np.int64), np.array([w[1] for w in v], dtype=bool),
                          np.array([w[2] for w in v], dtype=np.int64), np.array([w[3] for w in v], dtype=float))
                      for s, v in h["watch"].items()}
        self.entry = {}
        for s, rows in h["entry"].items():
            Tw = self.watch[s][0]
            T = np.array([r[0] for r in rows], dtype=np.int64)
            k = np.searchsorted(Tw, T, side="right") - 1
            k_ok = (k >= 0) & (Tw[np.maximum(k, 0)] == T)
            self.entry[s] = {"T": T, "k": np.maximum(k, 0), "k_ok": k_ok,
                             "eb": np.array([r[1] for r in rows], dtype=np.int64),
                             "c15": np.array([r[2] for r in rows], dtype=float),
                             "score": np.array([r[6] for r in rows], dtype=np.int64), "rows": rows}
        self.coil_cache: dict = dict(h.get("coil_cache", {}))
        self.disabled = set(h["disabled"])

    # candidates as (T, sym, kind, payload) for a combination
    def ign_candidates(self, need: int, thr: int) -> dict[str, list[tuple]]:
        ok = ((self.bits & need) == need) & (self.score >= thr)
        out = {}
        for s, (a, b) in self.seg.items():
            idx = np.flatnonzero(ok[a:b]) + a
            if len(idx):
                out[s] = [(int(self.T[i]), 0, int(i)) for i in idx]
        return out

    def coil_candidates(self, wneed: int, eneed: int, thr: int, valid_ms: int) -> dict[str, list[tuple]]:
        out = {}
        for s, e in self.entry.items():
            Tw, base, wb, box = self.watch[s]
            ok = base & ((wb & wneed) == wneed)
            last = np.maximum.accumulate(np.where(ok, np.arange(len(Tw)), -1))
            m = e["k_ok"] & ((e["eb"] & eneed) == eneed) & (e["score"] >= thr)
            j = last[e["k"]]
            m &= j >= 0
            jj = np.maximum(j, 0)
            m &= (e["T"] - Tw[jj] <= valid_ms) & (e["c15"] > box[jj])
            idx = np.flatnonzero(m)
            if len(idx):
                rows = e["rows"]
                out[s] = [(int(e["T"][i]), 1, (int(Tw[jj[i]]), float(box[jj[i]]), rows[i][4], rows[i][3], rows[i][5],
                                               rows[i][6], rows[i][7])) for i in idx]
        return out

    def coil_outcome(self, sym: str, T: int, payload: tuple) -> tuple:
        _, box, ref, atr, res, score, rstate = payload
        key = (sym, T, round(box, 12))
        if key not in self.coil_cache:
            cfg = self.bt.cfg
            stop = box - cfg.plan.coil_stop_atr15 * atr if atr is not None and not math.isnan(atr) else None
            self.coil_cache[key] = self.bt.coil_trade(sym, T, ref, stop, res, score, rstate, self.end)
        return self.coil_cache[key]

    def run(self, ign: dict, coil: dict, cooldown_ms: int, cap: int, cfg) -> list[dict]:
        """Exact live gating over the given candidates. Returns the SENT trades (Policy S)."""
        fired = []                         # (T, -score, sym, kind, payload/idx)
        for s in set(ign) | set(coil):
            ev = sorted((ign.get(s) or []) + (coil.get(s) or []), key=lambda e: (e[0], e[1]))
            last, consumed = -10 ** 15, -10 ** 15
            fired_at = None
            for T, kind, p in ev:
                if T - last < cooldown_ms or fired_at == T:
                    continue
                if kind == 1 and p[0] <= consumed:
                    continue              # this watch was used up by an earlier Coil signal
                last, fired_at = T, T
                if kind == 1:
                    consumed = T
                    fired.append((T, -p[5], s, 1, p))
                else:
                    fired.append((T, -int(self.score[p]), s, 0, p))
        return self._gate(fired, cap)

    def run_ign(self, need: int, thr: int, cooldown_ms: int, cap: int) -> list[dict]:
        """run() for Ignition alone, faster: the cooldown walk jumps from signal to signal."""
        ok = ((self.bits & need) == need) & (self.score >= thr)
        fired = []
        for s, (a, b) in self.seg.items():
            idx = np.flatnonzero(ok[a:b]) + a
            t = self.T[idx]
            i = 0
            while i < len(t):
                p = int(idx[i])
                fired.append((int(t[i]), -int(self.score[p]), s, 0, p))
                i = int(np.searchsorted(t, t[i] + cooldown_ms, side="left"))
        return self._gate(fired, cap)

    def _gate(self, fired: list[tuple], cap: int) -> list[dict]:
        """Alert gating in bar order, highest score first: Policy S must fit, then the hourly cap.
        Equal scores on the same bar: by symbol (live uses volume order; it only matters when the cap bites)."""
        fired.sort(key=lambda x: (x[0], x[1], x[2]))
        sent_times: deque = deque()
        out = []
        for T, negscore, s, kind, p in fired:
            if kind == 0:
                s_ok, rS, netS, exitS = bool(self.s_ok[p]), float(self.rS[p]), float(self.netS[p]), int(self.exitS[p])
                setup = "IGNITION"
            else:
                s_ok, _, _, rS, netS, exitS, *_ = self.coil_outcome(s, T, p)
                setup = "COIL"
            while sent_times and sent_times[0] <= T - H1:
                sent_times.popleft()
            if not s_ok:                    # default_policy_na (Policy S doesn't fit): suppressed
                continue
            if len(sent_times) >= cap:      # hourly cap
                continue
            sent_times.append(T)
            if not math.isnan(rS):
                out.append({"T": T, "sym": s, "setup": setup, "r": rS, "net": netS, "exit": exitS})
        return out


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "R": 0.0, "avgR": 0.0, "win": 0.0, "pf": 0.0, "net": 0.0, "maxdd": 0.0}
    tr = sorted(trades, key=lambda t: t["exit"] or t["T"])
    r = np.array([t["r"] for t in tr])
    net = np.array([t["net"] for t in tr])
    eq = np.cumsum(r)
    dd = float(np.max(np.maximum.accumulate(np.r_[0, eq]) - np.r_[0, eq]))
    gw, gl = net[net > 0].sum(), -net[net < 0].sum()
    return {"n": int(len(r)), "R": float(r.sum()), "avgR": float(r.mean()), "win": float((r > 0).mean()),
            "pf": float(gw / gl) if gl > 0 else math.inf, "net": float(net.sum()), "maxdd": dd}


def mask_of(keys: list[str], kept: tuple[str, ...]) -> int:
    return sum(1 << keys.index(k) for k in kept)


# ---- CLI ------------------------------------------------------------------------------------------

async def cmd_harvest(a) -> int:
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
    hv = Harvester(cfg, prep["inp"], prep["disabled"])
    t0 = time.time()
    _say(f"Harvesting {prep['days']} days, {len(prep['syms']) - 1} symbols (disabled groups: {sorted(prep['disabled'])})")
    res = hv.harvest(prep["start"], prep["end"],
                     lambda T, n, u: _say(f"  {clock.fmt(T, with_date=True)[:10]}: {n:,} Ignition candidates, universe {u}"))
    out = cfg.data_dir / cfg.backtest.report_dir / f"ablation_{a.tag}.pkl"
    with open(out, "wb") as f:
        pickle.dump({"harvest": res, "args": vars(a), "config_hash": cfg.hash}, f)
    _say(f"{len(res['ign']):,} Ignition candidates in {time.time() - t0:.0f}s -> {out}")
    return 0


def load_year(cfg, tag: str) -> Year:
    """Rebuild the harvester's data (for Coil trades on demand) from the cached history."""
    p = cfg.data_dir / cfg.backtest.report_dir / f"ablation_{tag}.pkl"
    with open(p, "rb") as f:
        blob = pickle.load(f)
    a = argparse.Namespace(**blob["args"])
    base = load_config(a.config)
    d = copy.deepcopy(base.to_dict())
    apply_overrides(d, a.set or [])
    c = Config(d, base.path)
    clock = Clock(c.app.display_tz)

    async def _prep():
        rest = WeexRest(c, clock)
        db = Database(c.database.url.format(data_dir=c.data_dir.as_posix()))
        try:
            return await prepare(c, rest, db, clock, a.days, a.symbols, None, a.set or [],
                                 end_ms=parse_local(a.end, c.app.display_tz) if a.end else None)
        finally:
            await rest.close()
    prep = asyncio.run(_prep())
    return Year(tag, blob["harvest"], Harvester(c, prep["inp"], prep["disabled"]))


def cmd_search(a) -> int:
    cfg = load_config(a.config)
    years = [load_year(cfg, t) for t in a.tags.split(",")]
    cd = int(cfg.signals.symbol_cooldown_min) * 60_000
    cap = int(cfg.signals.max_alerts_per_hour)
    valid = int(cfg.coil.watch_valid_h) * H1
    full_i, full_w, full_e = (1 << len(IGN_KEYS)) - 1, (1 << len(WATCH_KEYS)) - 1, (1 << len(ENTRY_KEYS)) - 1
    thr0 = int(cfg.score.min_entry)
    out_dir = cfg.data_dir / cfg.backtest.report_dir
    report: dict = {"years": [y.tag for y in years], "ign_keys": IGN_KEYS, "watch_keys": WATCH_KEYS,
                    "entry_keys": ENTRY_KEYS}

    # 0) the current system, exactly as live (checked against the normal backtest by the caller)
    cur = {}
    for y in years:
        cur[y.tag] = stats(y.run(y.ign_candidates(full_i, thr0), y.coil_candidates(full_w, full_e, thr0, valid), cd, cap, cfg))
        _say(f"[{y.tag}] current system: {cur[y.tag]}")
    report["current"] = cur
    if a.check:
        return 0

    # 1) Ignition on its own: every on/off combination x score cut-offs
    t0 = time.time()
    rows = []
    for r in range(len(IGN_KEYS) + 1):
        for kept in itertools.combinations(IGN_KEYS, r):
            need = mask_of(IGN_KEYS, kept)
            for thr in SCORES:
                res = {y.tag: stats(y.run_ign(need, thr, cd, cap)) for y in years}
                rows.append({"kept": kept, "thr": thr, **{t: v for t, v in res.items()}})
    _say(f"Ignition: {len(rows):,} combinations in {time.time() - t0:.0f}s")
    report["ignition"] = rows

    # 2) Coil on its own
    t0 = time.time()
    crow = []
    for rw in range(len(WATCH_KEYS) + 1):
        for kw in itertools.combinations(WATCH_KEYS, rw):
            for re_ in range(len(ENTRY_KEYS) + 1):
                for ke in itertools.combinations(ENTRY_KEYS, re_):
                    for thr in SCORES:
                        res = {y.tag: stats(y.run({}, y.coil_candidates(mask_of(WATCH_KEYS, kw), mask_of(ENTRY_KEYS, ke),
                                                                        thr, valid), cd, cap, cfg)) for y in years}
                        crow.append({"watch": kw, "entry": ke, "thr": thr, **res})
    _say(f"Coil: {len(crow):,} combinations in {time.time() - t0:.0f}s")
    report["coil"] = crow

    with open(out_dir / f"ablation_search_{'_'.join(y.tag for y in years)}.pkl", "wb") as f:
        pickle.dump(report, f)
    # keep simulated Coil trades for the next run
    for y in years:
        p = out_dir / f"ablation_{y.tag}.pkl"
        with open(p, "rb") as f:
            blob = pickle.load(f)
        blob["harvest"]["coil_cache"] = y.coil_cache
        with open(p, "wb") as f:
            pickle.dump(blob, f)
    _say("Saved search results.")
    return 0


def _row_flat(r: dict, years: list[str], keys: list[str], kept_field: str = "kept") -> dict:
    kept = set(r[kept_field]) if kept_field in r else set()
    out = {"removed": " ".join(k.split(".")[-1] for k in keys if k not in kept) or "-", "score_min": r["thr"]}
    for y in years:
        for m in ("n", "R", "avgR", "win", "pf", "net", "maxdd"):
            out[f"{y}_{m}"] = r[y][m]
    out["min_R"] = min(r[y]["R"] for y in years)
    return out


def cmd_report(a) -> int:
    """Rank the combinations: robust = good in BOTH years; 2-fold = picked on one year, tested on the other."""
    cfg = load_config(a.config)
    out_dir = cfg.data_dir / cfg.backtest.report_dir
    with open(out_dir / f"ablation_search_{a.tags.replace(',', '_')}.pkl", "rb") as f:
        rep = pickle.load(f)
    years = rep["years"]
    thr0 = int(cfg.score.min_entry)
    lines = []

    def fmt(r):
        return "  ".join(f"{y}: n={r[y]['n']:4d} R={r[y]['R']:+7.1f} avg={r[y]['avgR']:+.3f} win={r[y]['win'] * 100:3.0f}% "
                         f"PF={r[y]['pf']:.2f} DD={r[y]['maxdd']:5.1f}R ${r[y]['net']:+6.0f}" for y in years)

    for name, rows, keys in (("IGNITION", rep["ignition"], IGN_KEYS), ("COIL", rep["coil"], None)):
        lines.append(f"==== {name}: {len(rows):,} combinations ====")
        if name == "IGNITION":
            full = next(r for r in rows if len(r["kept"]) == len(IGN_KEYS) and r["thr"] == thr0)
            lines.append("current (all conditions, score >= %d): %s" % (thr0, fmt(full)))
            lines.append("remove ONE condition (score >= %d):" % thr0)
            for k in IGN_KEYS:
                r = next(x for x in rows if len(x["kept"]) == len(IGN_KEYS) - 1 and k not in x["kept"] and x["thr"] == thr0)
                d = "  ".join(f"{y}: {r[y]['R'] - full[y]['R']:+6.1f}R ({r[y]['n'] - full[y]['n']:+4d} trades)" for y in years)
                lines.append(f"  - {k:22s} {d}")
        elig = [r for r in rows if all(r[y]["n"] >= a.min_trades for y in years)]
        lines.append(f"{len(elig):,} combinations with >= {a.min_trades} trades in every year")
        robust = sorted(elig, key=lambda r: min(r[y]["R"] for y in years), reverse=True)
        lines.append("best in BOTH years (ranked by the weaker year's total R):")
        for r in robust[:15]:
            what = ("removed: " + (", ".join(k.split(".", 1)[1] for k in IGN_KEYS if k not in r["kept"]) or "none")
                    if name == "IGNITION" else
                    "watch kept: " + (", ".join(k.split(".")[-1] for k in r["watch"]) or "none") +
                    " | entry kept: " + (", ".join(k.split(".")[-1] for k in r["entry"]) or "none"))
            lines.append(f"  score>={r['thr']:2d} {what}")
            lines.append(f"      {fmt(r)}")
        for pick, test in (years, years[::-1]):
            best = max(elig, key=lambda r: r[pick]["R"])
            rank = sorted((r[test]["R"] for r in elig), reverse=True).index(best[test]["R"]) + 1
            lines.append(f"2-fold: best on {pick} -> {test}: R={best[test]['R']:+.1f} (rank {rank:,} of {len(elig):,} there)")
        if name == "IGNITION":
            top = robust[:100]
            lines.append("share of the 100 most robust combinations that KEEP each condition:")
            for k in IGN_KEYS:
                lines.append(f"  {k:22s} {sum(k in r['kept'] for r in top):3d}%")
    txt = "\n".join(lines)
    (out_dir / f"ablation_report_{a.tags.replace(',', '_')}.txt").write_text(txt, encoding="utf-8")
    with open(out_dir / f"ablation_ignition_{a.tags.replace(',', '_')}.csv", "w", newline="", encoding="utf-8") as f:
        flat = [_row_flat(r, years, IGN_KEYS) for r in rep["ignition"]]
        w = csv.DictWriter(f, fieldnames=list(flat[0]))
        w.writeheader()
        w.writerows(sorted(flat, key=lambda x: -x["min_R"]))
    print(txt)
    return 0


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Which Ignition / Coil conditions earn their keep?")
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("harvest")
    h.add_argument("--tag", required=True)
    h.add_argument("--days", type=int, default=365)
    h.add_argument("--end", default=None)
    h.add_argument("--symbols", default=None)
    h.add_argument("--config", default=None)
    h.add_argument("--set", action="append", metavar="KEY=VALUE")
    s = sub.add_parser("search")
    s.add_argument("--tags", required=True)
    s.add_argument("--config", default=None)
    s.add_argument("--check", action="store_true", help="only replay the current system (compare with a normal backtest)")
    r = sub.add_parser("report")
    r.add_argument("--tags", required=True)
    r.add_argument("--config", default=None)
    r.add_argument("--min-trades", type=int, default=40)
    a = ap.parse_args()
    if a.cmd == "harvest":
        return asyncio.run(cmd_harvest(a))
    if a.cmd == "report":
        return cmd_report(a)
    return cmd_search(a)


if __name__ == "__main__":
    sys.exit(main())
