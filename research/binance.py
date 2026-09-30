"""WEEX vs Binance: do Binance's volume, order flow and funding make better signals?

    .venv\\Scripts\\python.exe -m research.binance --history y2025,y2026

1. download  Binance USDT-M futures 5m klines (with taker-buy volume) and funding for the coins of
             the history dataset (free public market data, fapi.binance.com; cached in
             <data_dir>/research/binance/). Coins Binance doesn't list are reported and skipped.
2. compare   per coin and year: 5m return correlation, lead/lag, price gap, volume ratio,
             taker-flow correlation - WEEX vs Binance.
3. features  Binance versions of rvol_5m, rvol_15m, taker_15m, cvd_norm (the scanner's own
             formulas; checked to reproduce the recorded WEEX values first) and funding_8h, for
             every row of the history dataset.
4. systems   the current system with WEEX measures swapped for Binance ones, replayed on the
             breakout bars with the live alert rules and exact Policy S trades (research.candidates).
Execution stays on WEEX in every variant (entries, stops, targets use WEEX prices).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_config  # noqa: E402
from research.candidates import gate, mask, stats  # noqa: E402
from research.mine import _outcome, active_conds, edges, load_history  # noqa: E402

FAPI = "https://fapi.binance.com"
M5, M15, DAY = 300_000, 900_000, 86_400_000
FIELDS = ("t", "o", "h", "l", "c", "v", "qv", "tbv", "tbqv")


# ---- 1. download ---------------------------------------------------------------------------------

class Binance:
    def __init__(self, max_weight: int = 1800):
        self.c = httpx.AsyncClient(base_url=FAPI, timeout=30)
        self.max_weight = max_weight

    async def get(self, path: str, params: dict):
        for attempt in range(8):
            try:
                r = await self.c.get(path, params=params)
            except httpx.HTTPError:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            used = int(r.headers.get("x-mbx-used-weight-1m") or 0)
            if r.status_code in (418, 429):
                await asyncio.sleep(float(r.headers.get("retry-after") or 60))
                continue
            if used > self.max_weight:              # stay well under Binance's per-minute budget
                await asyncio.sleep(61 - time.time() % 60)
            if r.status_code == 400:
                return None                         # e.g. symbol not listed
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Binance {path}: gave up")

    async def listed(self) -> set[str]:
        info = await self.get("/fapi/v1/exchangeInfo", {})
        return {s["symbol"] for s in info["symbols"]
                if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"}

    async def klines(self, sym: str, start: int, end: int) -> np.ndarray:
        rows, t = [], start
        while t < end:
            got = await self.get("/fapi/v1/klines", {"symbol": sym, "interval": "5m", "startTime": t,
                                                      "endTime": end, "limit": 1000})
            if not got:
                break
            rows += [[float(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[7]),
                      float(k[9]), float(k[10])] for k in got if int(k[6]) < end]
            if len(got) < 1000:
                break
            t = int(got[-1][0]) + M5
        return np.array(rows) if rows else np.empty((0, 9))

    async def funding(self, sym: str, start: int, end: int) -> list[list[float]]:
        out, t = [], start
        while t < end:
            got = await self.get("/fapi/v1/fundingRate", {"symbol": sym, "startTime": t, "endTime": end, "limit": 1000})
            if not got:
                break
            out += [[int(x["fundingTime"]), float(x["fundingRate"])] for x in got]
            if len(got) < 1000:
                break
            t = int(got[-1]["fundingTime"]) + 1
        return out

    async def close(self):
        await self.c.aclose()


async def download(cfg, symbols: list[str], start: int, end: int) -> dict:
    d = Path(cfg.data_dir) / "research" / "binance"
    d.mkdir(parents=True, exist_ok=True)
    bn = Binance()
    try:
        listed = await bn.listed()
        todo = [s for s in symbols if s in listed]
        sem = asyncio.Semaphore(4)
        done = 0

        async def one(sym):
            nonlocal done
            p, pf = d / f"{sym}_5m.npz", d / f"{sym}_funding.json"
            async with sem:
                if not p.exists() or load_bn(p)[:, 0].max(initial=0) < end - 2 * M5:
                    np.savez_compressed(p, a=await bn.klines(sym, start, end))
                if not pf.exists():
                    pf.write_text(json.dumps(await bn.funding(sym, start - 3 * DAY, end)))
            done += 1
            print(f"  [{done}/{len(todo)}] {sym}", flush=True)
        await asyncio.gather(*(one(s) for s in todo))
    finally:
        await bn.close()
    return {"listed": todo, "not_on_binance": [s for s in symbols if s not in listed]}


def load_bn(p: Path) -> np.ndarray:
    with np.load(p) as z:
        return z["a"]


# ---- 3. features (vectorised copies of features.compute) ------------------------------------------------

def flow_features(t: np.ndarray, qv: np.ndarray, tbqv: np.ndarray, cfg) -> pd.DataFrame:
    """rvol_5m, rvol_15m, taker_15m, cvd_norm for every 5m bar (index = bar CLOSE time)."""
    f = cfg.features
    n5, n15, nt, nc = int(f.rvol_5m_lookback), int(f.rvol_15m_lookback), int(f.taker_ratio_bars_5m), int(f.cvd_bars_5m)
    n = len(t)
    cs = np.r_[0, np.cumsum(qv)]
    prev_mean = np.full(n, np.nan)
    i = np.arange(n5, n)
    prev_mean[i] = (cs[i] - cs[i - n5]) / n5                        # mean of the n5 bars before bar i
    rvol5 = np.where(prev_mean > 0, qv / prev_mean, np.nan)
    ctb = np.r_[0, np.cumsum(tbqv)]
    j = np.arange(nt - 1, n)
    tak = np.full(n, np.nan)
    tot = cs[j + 1] - cs[j + 1 - nt]
    tak[j] = np.where(tot > 0, (ctb[j + 1] - ctb[j + 1 - nt]) / np.where(tot > 0, tot, 1), np.nan)
    # CVD slope over the last nc bars: linreg slope of the running sum = sum_j delta_j * c_j
    x = np.arange(nc, dtype=float) - (nc - 1) / 2
    w = x / (x * x).sum()
    cw = np.cumsum(w[::-1])[::-1]                                     # c_j = sum_{k>=j} w_k
    delta = 2 * tbqv - qv
    cvd = np.full(n, np.nan)
    if n >= nc:
        win = np.lib.stride_tricks.sliding_window_view(delta, nc)
        slope = win @ cw
        mq = np.lib.stride_tricks.sliding_window_view(qv, nc).mean(axis=1)
        cvd[nc - 1:] = np.where(mq > 0, slope / np.where(mq > 0, mq, 1), np.nan)
    # 15m bars: complete groups of three 5m bars; rvol_15m uses the last CLOSED 15m bar
    k15 = t - t % M15
    df15 = pd.DataFrame({"k": k15, "qv": qv}).groupby("k").agg(qv=("qv", "sum"), n=("qv", "size"))
    df15 = df15[df15["n"] == 3]
    q15 = df15["qv"].to_numpy()
    c15 = np.r_[0, np.cumsum(q15)]
    m15 = np.full(len(q15), np.nan)
    ii = np.arange(n15, len(q15))
    m15[ii] = (c15[ii] - c15[ii - n15]) / n15
    rv15 = np.where(m15 > 0, q15 / m15, np.nan)
    close15 = df15.index.to_numpy() + M15
    tc = t + M5
    pos = np.searchsorted(close15, tc, side="right") - 1
    rvol15 = np.where(pos >= 0, rv15[np.maximum(pos, 0)], np.nan)
    return pd.DataFrame({"ts": tc.astype(np.int64), "rvol_5m": rvol5, "rvol_15m": rvol15, "taker_15m": tak,
                         "cvd_norm": cvd})


def funding_8h(ts: np.ndarray, rows: list[list[float]], norm_min: int) -> np.ndarray:
    """Last settled Binance rate at or before each time, normalised to 8h, in % (like funding_8h)."""
    if not rows:
        return np.full(len(ts), np.nan)
    ft = np.array([r[0] for r in rows], dtype=np.int64)
    fr = np.array([r[1] for r in rows])
    i = np.searchsorted(ft, ts, side="right") - 1
    ok = i >= 1
    iv = np.where(ok, (ft[np.maximum(i, 1)] - ft[np.maximum(i, 1) - 1]) / 60_000, 480)
    return np.where(i >= 0, fr[np.maximum(i, 0)] * norm_min / np.maximum(iv, 1) * 100, np.nan)


# ---- main ------------------------------------------------------------------------------------------------

def weex_arrays(cfg, sym: str) -> dict | None:
    p = Path(cfg.data_dir) / cfg.backtest.cache_dir / f"{sym}_5m.npz"
    if not p.exists():
        return None
    with np.load(p) as z:
        return {k: z[k] for k in ("t", "c", "v", "qv", "tbqv")}


def compare(cfg, sym: str, bn: np.ndarray, periods: dict[str, tuple[int, int]]) -> dict:
    w = weex_arrays(cfg, sym)
    if w is None or not len(bn):
        return {}
    bt = bn[:, 0].astype(np.int64)
    common, iw, ib = np.intersect1d(w["t"].astype(np.int64), bt, return_indices=True)
    out = {}
    for name, (a, b) in periods.items():
        m = (common >= a) & (common < b)
        if m.sum() < 1000:
            continue
        wi, bi = iw[m], ib[m]
        wc, bc = w["c"][wi], bn[bi, 4]
        wr, br = np.diff(np.log(wc)), np.diff(np.log(bc))
        wq, bq = w["qv"][wi], bn[bi, 6]
        wtb = np.convolve(w["tbqv"][wi], np.ones(3), "valid") / np.maximum(np.convolve(wq, np.ones(3), "valid"), 1e-12)
        btb = np.convolve(bn[bi, 8], np.ones(3), "valid") / np.maximum(np.convolve(bq, np.ones(3), "valid"), 1e-12)
        ok = np.isfinite(wtb) & np.isfinite(btb)
        out[name] = {
            "bars": int(m.sum()),
            "ret_corr": round(float(np.corrcoef(wr, br)[0, 1]), 4),
            "weex_after_binance": round(float(np.corrcoef(wr[1:], br[:-1])[0, 1]), 3),   # >0 = Binance leads
            "binance_after_weex": round(float(np.corrcoef(br[1:], wr[:-1])[0, 1]), 3),
            "price_gap_pct": round(float(np.median(np.abs(wc / bc - 1)) * 100), 4),
            "volume_ratio": round(float(wq.sum() / bq.sum()), 3),
            "weex_qv_vs_price_x_vol": round(float(np.median(wq / np.maximum(w["v"][wi] * wc, 1e-12))), 3),
            "taker_15m_corr": round(float(np.corrcoef(wtb[ok], btb[ok])[0, 1]), 3) if ok.sum() > 100 else None,
        }
    return out


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="WEEX vs Binance signal study")
    ap.add_argument("--history", default="y2025,y2026")
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    cfg = load_config(a.config)
    df = load_history(cfg, a.history.split(","))
    syms = sorted(df["symbol"].unique())
    start, end = int(df["ts"].min()) - 8 * DAY, int(df["ts"].max()) + 2 * M5
    periods = {p: (int(g["ts"].min()), int(g["ts"].max())) for p, g in df.groupby("period")}
    t0 = time.time()
    print(f"1. Binance data for {len(syms)} coins...", flush=True)
    cov = asyncio.run(download(cfg, syms, start, end))
    print(f"   {len(cov['listed'])} on Binance; not listed there: {cov['not_on_binance']}  ({time.time() - t0:.0f}s)")
    bdir = Path(cfg.data_dir) / "research" / "binance"

    print("2. Comparing WEEX and Binance...", flush=True)
    cmp_ = {s: compare(cfg, s, load_bn(bdir / f"{s}_5m.npz"), periods) for s in cov["listed"]}

    print("3. Binance features (checking the formulas on WEEX first)...", flush=True)
    feats, check = [], []
    norm = int(cfg.funding.normalise_to_min)
    for s in cov["listed"]:
        bn = load_bn(bdir / f"{s}_5m.npz")
        if not len(bn):
            continue
        fb = flow_features(bn[:, 0].astype(np.int64), bn[:, 6], bn[:, 8], cfg).add_prefix("bn_").rename(columns={"bn_ts": "ts"})
        fb["bn_funding_8h"] = funding_8h(fb["ts"].to_numpy(), json.loads((bdir / f"{s}_funding.json").read_text()), norm)
        fb["symbol"] = s
        feats.append(fb[fb["ts"].isin(df.loc[df["symbol"] == s, "ts"])])    # only the rows we analyse
        w = weex_arrays(cfg, s)
        if w is not None:
            fw = flow_features(w["t"].astype(np.int64), w["qv"], w["tbqv"], cfg)
            rec = df[df["symbol"] == s][["ts", "rvol_5m", "rvol_15m", "taker_15m", "cvd_norm"]].merge(fw, on="ts", suffixes=("", "_v"))
            for k in ("rvol_5m", "rvol_15m", "taker_15m", "cvd_norm"):
                x, y = rec[k].to_numpy(float), rec[k + "_v"].to_numpy(float)
                m = np.isfinite(x) & np.isfinite(y)
                check.append((k, int(m.sum()), float(np.mean(np.isclose(x[m], y[m], rtol=1e-4, atol=1e-6))) if m.any() else math.nan))
    chk = pd.DataFrame(check, columns=["feature", "rows", "match"]).groupby("feature").apply(
        lambda g: round(float(np.average(g["match"], weights=g["rows"])), 5) if g["rows"].sum() else None).to_dict()
    print(f"   formula check (share of recorded WEEX values reproduced): {chk}")
    fb = pd.concat(feats, ignore_index=True)
    d = df.merge(fb, on=["symbol", "ts"], how="left")
    have = d["bn_rvol_5m"].notna()
    print(f"   Binance values for {have.mean() * 100:.0f}% of rows")

    print("4. Signal value on breakouts...", flush=True)
    b = d[d["c_break"] == 1].copy()
    tgt = "trade_r"
    pairs = [("rvol_5m", "bn_rvol_5m"), ("rvol_15m", "bn_rvol_15m"), ("taker_15m", "bn_taker_15m"),
             ("cvd_norm", "bn_cvd_norm"), ("funding_8h", "bn_funding_8h")]
    bb = b[b["bn_rvol_5m"].notna()]
    ed = {e["feature"]: e for e in edges(bb, tgt, [x for p in pairs for x in p])}
    cd, cap, thr = int(cfg.signals.symbol_cooldown_min) * 60_000, int(cfg.signals.max_alerts_per_hour), int(cfg.score.min_entry)
    act = active_conds(cfg)
    g_ = cfg.ignition

    def current(g, drop=()):
        m = g["score"] >= thr
        for c in act:
            if c not in drop and (g[c] == -1).mean() < 0.95:
                m &= g[c] == 1
        return m

    def bn_or(g, col, test, weex_col):
        """Binance version where Binance lists the coin; the WEEX condition otherwise."""
        has = g[col].notna()
        return (has & test(g[col])) | (~has & (g[weex_col] == 1))

    cvd_bn = lambda g: bn_or(g, "bn_cvd_norm", lambda x: x > float(g_.min_cvd_slope), "c_cvd")      # noqa: E731
    rvol_bn = lambda g: bn_or(g, "bn_rvol_5m", lambda x: x >= float(g_.min_rvol_5m), "c_rvol_5m")  # noqa: E731
    fund_bn = lambda g: g["bn_funding_8h"].isna() | (g["bn_funding_8h"] <= float(g_.max_funding_8h_pct))  # noqa: E731
    systems = {
        "Current system (WEEX measures)": lambda g: current(g),
        "CVD from Binance": lambda g: current(g, ("c_cvd",)) & cvd_bn(g),
        "5m volume spike from Binance": lambda g: current(g, ("c_rvol_5m",)) & rvol_bn(g),
        "CVD + volume spike from Binance": lambda g: current(g, ("c_cvd", "c_rvol_5m")) & cvd_bn(g) & rvol_bn(g),
        "Current + Binance taker >= 0.55": lambda g: current(g) & (g["bn_taker_15m"].isna() | (g["bn_taker_15m"] >= float(g_.min_taker_ratio_15m))),
        "Funding from Binance (testable in both years)": lambda g: current(g, ("c_funding",)) & fund_bn(g),
        "All flow + funding from Binance": lambda g: current(g, ("c_cvd", "c_rvol_5m", "c_funding")) & cvd_bn(g) & rvol_bn(g) & fund_bn(g),
    }
    res = {}
    for name, fn in systems.items():
        res[name] = {p: stats(gate(g[fn(g)], cd, cap)) for p, g in b.groupby("period")}
    cond_cmp = {}
    for lab, wx, bnx in (("rvol_5m >= 3", b["c_rvol_5m"] == 1, b["bn_rvol_5m"] >= float(g_.min_rvol_5m)),
                         ("cvd > 0", b["c_cvd"] == 1, b["bn_cvd_norm"] > float(g_.min_cvd_slope)),
                         ("taker >= 0.55", b["c_taker"] == 1, b["bn_taker_15m"] >= float(g_.min_taker_ratio_15m))):
        sub = b["bn_rvol_5m"].notna()
        cond_cmp[lab] = {p: {"weex_pass": _outcome(g[(wx & sub).loc[g.index]]), "binance_pass": _outcome(g[(bnx & sub).loc[g.index]])}
                         for p, g in b.groupby("period")}

    rep = {"generated_ms": int(time.time() * 1000), "coverage": cov, "formula_check": chk, "compare": cmp_,
           "edges": {k: {"weex": ed.get(k), "binance": ed.get(v)} for k, v in pairs}, "conditions": cond_cmp,
           "systems": res}
    out = Path(cfg.data_dir) / "research" / "reports" / "binance.json"
    out.write_text(json.dumps(rep, indent=1, default=lambda x: None), encoding="utf-8")
    fb_out = Path(cfg.data_dir) / "research" / "history" / "binance_features.csv.gz"
    fb.to_csv(fb_out, index=False)

    # ---- summary
    cm = pd.DataFrame([{"coin": s, "period": p, **v} for s, x in cmp_.items() for p, v in x.items()])
    print("\nWEEX vs Binance, median over coins:")
    print(cm.groupby("period")[["ret_corr", "weex_after_binance", "binance_after_weex", "price_gap_pct", "volume_ratio",
                                "taker_15m_corr"]].median().round(4).to_string())
    odd = cm[(cm["weex_qv_vs_price_x_vol"] - 1).abs() > 0.2]
    if len(odd):
        print("WEEX quote volume inconsistent with price x volume:", sorted(odd["coin"].unique()))
    print("\nWhich version predicts breakout trades better (rank correlation of deciles with trade R, per year):")
    for k, v in pairs:
        e1, e2 = ed.get(k) or {}, ed.get(v) or {}
        print(f"  {k:11s} WEEX rho={e1.get('rho')} consistent={e1.get('consistent')} | Binance rho={e2.get('rho')} consistent={e2.get('consistent')}")
    print("\nSame condition, measured on each exchange (breakouts passing it -> avg trade R):")
    for lab, x in cond_cmp.items():
        print(f"  {lab:14s} " + " | ".join(f"{p}: WEEX n={v['weex_pass']['n']} R={v['weex_pass'].get('trade_r')}"
                                            f"  Binance n={v['binance_pass']['n']} R={v['binance_pass'].get('trade_r')}" for p, v in x.items()))
    print("\nSystems (live alert rules, exact Policy S trades):")
    for name, r in res.items():
        print(f"  {name}")
        for p, x in r.items():
            print(f"     {p}: " + (f"n={x['n']:4d} R={x['R']:+7.1f} avg={x['avgR']:+.3f} PF={x['pf']} net=${x['net']:+,} DD={x['maxdd_R']}R" if x.get("n") else "no trades"))
    print(f"\nSaved: {out}  ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
