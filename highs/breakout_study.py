"""Intraday study of 52-week / all-history high breakouts: how far and how fast they run after
the alert, how deep they dip first, which exits would have worked, and what the big runners
had in common.

    .venv\\Scripts\\python.exe -m highs.breakout_study collect      (resumable; rerun until "all done")
    .venv\\Scripts\\python.exe -m highs.breakout_study futures      (funding + open interest, resumable)
    .venv\\Scripts\\python.exe -m highs.breakout_study report

Data: Binance public market data (free, no key). Spot USDT pairs INCLUDING delisted ones (their
history stays available), so coins that later died are in the sample.
- daily candles per pair -> the breaks, by the live scanner's rules (highs.detector): "AH" = the
  day's HIGH trades above the highest price of the pair's whole Binance history (an all-time
  high as far as Binance's history goes), that high being at least highs.min_high_age_days old;
  otherwise "52W" = above the highest high of the previous 365 days, the same age rule. A new
  all-history high whose old high is younger than that is a trend, not a break (no event).
  A pair needs a year of history first.
- 5-minute candles from 2h before the break day: the break moment = the first 5m bar whose high
  is above the old high. ENTRY = that bar's close (the live scan alerts within 10 minutes).
  Measured again entering 15 minutes, 1 hour and 4 hours later, and ("lvl") as a buy-stop order
  resting AT the old high: filled at the old high (or the bar's open if it gapped above) plus
  LEVEL_SLIP_PCT, with the whole breaking bar - also its low, which may have come before the
  fill - counted against the trade.
- hourly candles from 7 days before to ~34 days after.
- control: for every break, a random other pair at the same moment (same market, no breakout).
- futures (optional): funding rate and 5-minute open interest from data.binance.vision.
Nothing after the entry is used for the "features"; everything measured after it is an outcome.
Only compact per-event results are stored (<data_dir>/highs_cache/breakouts.jsonl), not candles.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_config  # noqa: E402

BINANCE = "https://data-api.binance.vision/api/v3"
VISION = "https://data.binance.vision/data/futures/um"
M5, H1, DAY = 300_000, 3_600_000, 86_400_000
PRE5 = 24                                   # 5m bars fetched before the break day (2h)
UP = [1, 2, 3, 5, 8, 10, 15, 20, 30, 50, 100]          # % above entry: first time reached
DN = [1, 1.5, 2, 3, 4, 5, 7, 10, 15, 20, 30]           # % below entry: first time reached
H5 = {"1h": 12, "4h": 48, "12h": 144, "24h": 288, "48h": 576}
DELAYS = {"d0": 0, "d15": 3, "d60": 12, "d4h": 48}     # entry delay in 5m bars after the breaking bar
NOT_COINS = {"EUR", "GBP", "AUD", "JPY", "BRL", "TRY", "RUB", "UAH", "NGN", "ZAR", "IDRT", "BIDR", "BKRW", "BVND",
             "PAX", "USDS", "USDSB", "AEUR", "EURI", "XUSD", "BFUSD", "PAXG", "XAUT", "WBTC", "WBETH", "BETH", "BNSOL",
             "USDSOLD", "VAI", "BTTC"}
COLS = 7                                    # t, o, h, l, c, quote volume, taker-buy quote volume
LEVEL_SLIP_PCT = 0.1                        # slippage assumed on a buy-stop order triggered at the old high


def day_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# ---- data ------------------------------------------------------------------------------------------

class Net:
    def __init__(self, conc: int = 10):
        self.c = httpx.AsyncClient(timeout=30)
        self.sem = asyncio.Semaphore(conc)
        self.requests = 0

    async def close(self) -> None:
        await self.c.aclose()

    async def klines(self, pair: str, interval: str, start: int, limit: int = 1000) -> np.ndarray:
        async with self.sem:
            for attempt in range(6):
                try:
                    r = await self.c.get(f"{BINANCE}/klines", params={"symbol": pair, "interval": interval,
                                                                      "startTime": start, "limit": limit})
                except httpx.HTTPError:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                self.requests += 1
                if r.status_code in (418, 429):
                    wait = float(r.headers.get("retry-after") or 30)
                    print(f"  Binance rate limit ({r.status_code}), waiting {wait:.0f}s", flush=True)
                    await asyncio.sleep(wait)
                    continue
                if r.status_code == 400:
                    return np.empty((0, COLS))
                if r.status_code >= 500:
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                r.raise_for_status()
                if int(r.headers.get("x-mbx-used-weight-1m") or 0) > 4500:      # limit 6,000 a minute
                    await asyncio.sleep(10)
                rows = r.json()
                if not rows:
                    return np.empty((0, COLS))
                return np.array([[k[0], k[1], k[2], k[3], k[4], k[7], k[10]] for k in rows], float)
            raise RuntimeError(f"Binance klines {pair} {interval}: giving up")

    async def vision_csv(self, path: str) -> list[list[str]] | None:
        """Rows of the one CSV inside a data.binance.vision zip; None if the file does not exist."""
        async with self.sem:
            for attempt in range(5):
                try:
                    r = await self.c.get(f"{VISION}/{path}")
                except httpx.HTTPError:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                self.requests += 1
                if r.status_code == 404:
                    return None
                if r.status_code != 200:
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                z = zipfile.ZipFile(io.BytesIO(r.content))
                return [ln.split(",") for ln in z.read(z.namelist()[0]).decode().splitlines()]
            return None


async def all_pairs(net: Net, stables: set[str]) -> list[str]:
    """Every USDT spot pair Binance ever listed that is a coin (no stablecoins, fiat, leveraged tokens)."""
    r = await net.c.get(f"{BINANCE}/exchangeInfo")
    r.raise_for_status()
    bases = {s["baseAsset"]: s["symbol"] for s in r.json()["symbols"] if s["quoteAsset"] == "USDT"}
    out = []
    for b, sym in bases.items():
        lev = any(b.endswith(x) and (b[:-len(x)] + y) in bases for x, y in (("UP", "DOWN"), ("DOWN", "UP"),
                                                                             ("BULL", "BEAR"), ("BEAR", "BULL")))
        if lev or b in stables or b in NOT_COINS:
            continue
        out.append(sym)
    return sorted(out)


async def daily(net: Net, pair: str, cache: Path, now_ms: int) -> np.ndarray:
    """Closed daily candles, cached and topped up."""
    p = cache / f"{pair}.npy"
    have = np.load(p) if p.exists() else np.empty((0, COLS))
    start = int(have[-1, 0]) + DAY if len(have) else 0
    new = []
    while start + DAY <= now_ms:
        got = await net.klines(pair, "1d", start)
        if not len(got):
            break
        new.append(got[got[:, 0] + DAY <= now_ms])
        if len(got) < 1000:
            break
        start = int(got[-1, 0]) + DAY
    if new and sum(len(x) for x in new):
        have = np.vstack([have, *new])
        np.save(p, have)
    return have


# ---- breaks ----------------------------------------------------------------------------------------

def find_breaks(d: np.ndarray, min_age: int) -> list[tuple[int, str, float, int]]:
    """(day index, kind, old high, index of the old high) for every break (see the module doc).
    The previous 365 days must be contiguous (no halt / relisting gap)."""
    t, h = d[:, 0], d[:, 2]
    out = []
    if len(d) <= 365:
        return out
    ath_i = int(np.argmax(h[:365]))
    for i in range(365, len(d)):
        if t[i] - t[i - 365] == 365 * DAY:
            if h[i] > h[ath_i]:
                if t[i] - t[ath_i] >= min_age * DAY:
                    out.append((i, "AH", float(h[ath_i]), ath_i))
            else:
                w = h[i - 365:i]
                j = int(np.argmax(w))
                if h[i] > w[j] and 365 - j >= min_age:
                    out.append((i, "52W", float(w[j]), i - 365 + j))
        if h[i] > h[ath_i]:
            ath_i = i
    return out


def first_at(mask: np.ndarray) -> int:
    k = int(np.argmax(mask))
    return k if mask[k] else -1


def measure(m5: np.ndarray, h1: np.ndarray, k: int, old: float, level: bool = False) -> dict | None:
    """Outcomes entering at the close of 5m bar k - or, with level=True, by a buy-stop order at the
    old high inside bar k. Times are in 5m bars (48h part) or hours (30d part)."""
    first = k if level else k + 1
    post = m5[first:first + H5["48h"]]
    if len(post) < H5["48h"] * 0.95 or post[-1, 0] - m5[k, 0] > 49 * H1:
        return None                                 # trading stopped inside the window
    E = float(max(old, m5[k, 1]) * (1 + LEVEL_SLIP_PCT / 100)) if level else float(m5[k, 4])
    hi, lo, cl = post[:, 2] / E - 1, post[:, 3] / E - 1, post[:, 4] / E - 1
    o: dict = {"entry": E, "gap": (E / old - 1) * 100}
    for name, n in H5.items():
        o[f"mfe_{name}"] = float(hi[:n].max() * 100)
        o[f"mae_{name}"] = float(lo[:n].min() * 100)
        o[f"ret_{name}"] = float(cl[min(n, len(cl)) - 1] * 100)
    pk = int(hi.argmax())
    o["t_peak48"] = pk
    o["dip_before_peak48"] = float(lo[:pk + 1].min() * 100)
    o["up48"] = [first_at(hi >= u / 100) for u in UP]
    o["dn48"] = [first_at(lo <= -x / 100) for x in DN]
    o["below_old48"] = first_at(post[:, 4] < old)            # first 5m close back under the old high
    before = m5[max(k - 11, 0):k + 1]
    o["vol_1h_after_vs_before"] = float(post[:12, 5].sum() / max(before[:, 5].sum() * 12 / len(before), 1e-9))
    # ---- 30 days, hourly bars that open after the 48h window (the 5m part covers the start)
    t_end48 = post[-1, 0] + M5
    hh = h1[(h1[:, 0] >= t_end48) & (h1[:, 0] < m5[k, 0] + 30 * DAY)]
    if len(hh) >= 28 * 24 * 0.95 and hh[-1, 0] >= m5[k, 0] + 29 * DAY:
        hi30, lo30 = hh[:, 2] / E - 1, hh[:, 3] / E - 1
        hours = (hh[:, 0] - m5[k, 0]) / H1

        def first30(level_hit48: int, mask: np.ndarray) -> float:
            if level_hit48 >= 0:
                return round((level_hit48 + 1) * 5 / 60, 2)
            j = first_at(mask)
            return round(float(hours[j]) + 1, 2) if j >= 0 else -1.0
        o["up30"] = [first30(a, hi30 >= u / 100) for a, u in zip(o["up48"], UP)]
        o["dn30"] = [first30(a, lo30 <= -x / 100) for a, x in zip(o["dn48"], DN)]
        m48, m30 = float(hi.max()), float(hi30.max())
        o["mfe_30d"] = max(m48, m30) * 100
        o["mae_30d"] = min(float(lo.min()), float(lo30.min())) * 100
        o["t_peak30_h"] = round((pk + 1) * 5 / 60, 2) if m48 >= m30 else round(float(hours[int(hi30.argmax())]) + 1, 2)
        for name, days in (("7d", 7), ("30d", 30)):
            j = np.searchsorted(hh[:, 0], m5[k, 0] + days * DAY - H1, side="right") - 1
            c = float(hh[max(j, 0), 4])
            o[f"ret_{name}"] = (c / E - 1) * 100
            o[f"above_old_{name}"] = bool(c > old)
    o["above_old_24h"] = bool(post[min(H5["24h"], len(post)) - 1, 4] > old)
    return {a: (round(b, 3) if isinstance(b, float) and a != "entry" else b) for a, b in o.items()}


def features(d: np.ndarray, i: int, j: int, old: float, m5: np.ndarray, h1: np.ndarray, k: int,
             btc: np.ndarray) -> dict:
    """What was known at the entry (nothing after bar k)."""
    E, t0 = float(m5[k, 4]), m5[k, 0]
    f: dict = {"age_days": int((d[i, 0] - d[j, 0]) // DAY), "hour": int((t0 % DAY) // H1), "dow": int(((t0 // DAY) + 4) % 7)}
    c, h, l, qv = d[:, 4], d[:, 2], d[:, 3], d[:, 5]
    f["liq_usd_30d"] = float(qv[i - 30:i].mean())
    f["atr_pct_14d"] = float(((h[i - 14:i] - l[i - 14:i]) / c[i - 14:i]).mean() * 100)
    f["base_depth_pct"] = float((l[j:i].min() / old - 1) * 100)          # deepest pullback since the old high
    f["near_days_30"] = int((h[i - 30:i] >= old * 0.95).sum())            # days spent within 5% of the old high
    f["ret_7d_pct"] = float((E / c[i - 8] - 1) * 100)
    f["ret_30d_pct"] = float((E / c[i - 31] - 1) * 100)
    f["above_low_52w_pct"] = float((E / l[i - 365:i].min() - 1) * 100)
    pre1 = h1[h1[:, 0] + H1 <= t0 + M5]                                   # hours closed by the entry
    if len(pre1) >= 48:
        hour_avg = float(pre1[-168:, 5].mean())
        last24 = float(pre1[-24:, 5].sum())
        f["rvol_24h"] = last24 / max(float(qv[i - 30:i].mean()), 1e-9)   # last 24h vs the 30-day daily average
        f["ret_24h_pct"] = float((E / pre1[-24, 1] - 1) * 100)
        pre5 = m5[max(k - 11, 0):k + 1]                                    # the hour ending at the entry
        v1 = float(pre5[:, 5].sum()) * 12 / len(pre5)
        f["rvol_1h"] = v1 / max(hour_avg, 1e-9)
        f["rvol_break_bar"] = float(m5[k, 5]) * 12 / max(hour_avg, 1e-9)
        f["buy_share_1h"] = float(pre5[:, 6].sum() / max(pre5[:, 5].sum(), 1e-9))   # taker buys / all volume
        f["buy_share_24h"] = float(pre1[-24:, 6].sum() / max(last24, 1e-9))
    f["bar_body_pct"] = float((m5[k, 4] / m5[k, 1] - 1) * 100)             # the breaking 5m candle
    bt, bc = btc[:, 0], btc[:, 4]
    b = int(np.searchsorted(bt, d[i, 0])) - 1                              # BTC's last closed day
    if b >= 200:
        f["btc_above_200d"] = bool(bc[b] > bc[b - 199:b + 1].mean())
        f["btc_ret_7d_pct"] = float((bc[b] / bc[b - 7] - 1) * 100)
        f["btc_ret_30d_pct"] = float((bc[b] / bc[b - 30] - 1) * 100)
    return {a: (round(v, 4) if isinstance(v, float) else v) for a, v in f.items()}


async def one_event(net: Net, ev: dict, d: np.ndarray, btc: np.ndarray) -> dict:
    """Fetch the event's candles and measure it. ev: pair, i, kind, old, j, control (bool), slot."""
    t_day = int(d[ev["i"], 0])
    m5, h1 = await asyncio.gather(net.klines(ev["pair"], "5m", t_day - PRE5 * M5),
                                  net.klines(ev["pair"], "1h", t_day - 7 * DAY))
    rec = {"key": ev["key"], "pair": ev["pair"], "kind": ev["kind"], "t_day": t_day, "old": ev["old"]}
    if ev.get("of"):
        rec["of"] = ev["of"]
    in_day = np.where((m5[:, 0] >= t_day) & (m5[:, 0] < t_day + DAY))[0] if len(m5) else []
    if not len(in_day):
        return {**rec, "skip": "no 5m data"}
    if ev["kind"] == "CTRL":
        k = int(in_day[0]) + ev["slot"]
        if k >= len(m5) or m5[k, 0] != t_day + ev["slot"] * M5:
            return {**rec, "skip": "no bar at the matched moment"}
    else:
        hit = np.where(m5[in_day, 2] > ev["old"])[0]
        if not len(hit):
            return {**rec, "skip": "5m data never crosses the daily high"}
        k = int(in_day[hit[0]])
        rec["slot"] = int(hit[0])
    rec["t"] = int(m5[k, 0] + M5)
    rec["f"] = features(d, ev["i"], ev["j"], ev["old"], m5, h1, k, btc)
    for name, delay in DELAYS.items():
        if k + delay < len(m5):
            m = measure(m5, h1, k + delay, ev["old"])
            if m:
                rec[name] = m
    if ev["kind"] != "CTRL":
        m = measure(m5, h1, k, ev["old"], level=True)
        if m:
            rec["lvl"] = m
    if "d0" not in rec:
        rec["skip"] = "trading stopped within 48h"
    return rec


# ---- stages ----------------------------------------------------------------------------------------

def paths(cfg) -> tuple[Path, Path, Path]:
    base = Path(cfg.data_dir) / "highs_cache"
    (base / "daily").mkdir(parents=True, exist_ok=True)
    return base / "daily", base / "breakouts.jsonl", base / "breakouts_futures.jsonl"


def read_jsonl(p: Path) -> dict[str, dict]:
    out = {}
    if p.exists():
        with open(p, encoding="utf-8") as fh:
            for ln in fh:
                if ln.strip():
                    r = json.loads(ln)
                    out[r["key"]] = r
    return out


async def collect(cfg, budget_s: float, limit: int | None) -> bool:
    t_start = time.time()
    now = int(time.time() * 1000)
    dcache, out_p, _ = paths(cfg)
    net = Net()
    try:
        pairs = await all_pairs(net, {s.upper() for s in cfg.universe.stablecoin_bases})
        print(f"{len(pairs)} USDT spot pairs (delisted ones included); daily history...", flush=True)
        data: dict[str, np.ndarray] = {}

        async def load(p: str) -> None:
            data[p] = await daily(net, p, dcache, now)
        await asyncio.gather(*(load(p) for p in pairs))
        btc = data["BTCUSDT"]
        min_age = int(cfg.highs.min_high_age_days)
        events = []
        for p in pairs:
            d = data[p]
            for i, kind, old, j in find_breaks(d, min_age):
                if d[i, 0] + 3 * DAY <= now:                 # 48h after the break must be over
                    events.append({"key": f"{p}:{day_str(int(d[i, 0]))}", "pair": p, "i": i, "kind": kind,
                                   "old": old, "j": j})
        events.sort(key=lambda e: (data[e["pair"]][e["i"], 0], e["pair"]))
        if limit:
            events = events[:: max(len(events) // limit, 1)][:limit]
        done = read_jsonl(out_p)
        print(f"{len(events)} breaks on {len({e['pair'] for e in events})} pairs; {len(done)} records already stored",
              flush=True)
        rng = np.random.default_rng(7)
        idx_of = {p: {int(t): n for n, t in enumerate(data[p][:, 0])} for p in pairs}
        break_days = {(e["pair"], int(data[e["pair"]][e["i"], 0])) for e in events}
        todo = [e for e in events if e["key"] not in done]
        ctrl_draws = {e["key"]: rng.permutation(len(pairs))[:40] for e in events}    # fixed per event (resumable)
        fh = open(out_p, "a", encoding="utf-8")
        n_new = 0

        async def run(ev: dict) -> None:
            try:
                await run_event(ev)
            except Exception as e:  # noqa: BLE001 - one event failing never stops the run (it is retried next run)
                print(f"  {ev['key']}: {type(e).__name__}: {e}", flush=True)

        async def run_event(ev: dict) -> None:
            nonlocal n_new
            rec = await one_event(net, ev, data[ev["pair"]], btc)
            fh.write(json.dumps(rec) + "\n")
            n_new += 1
            if ev["kind"] == "CTRL" or "slot" not in rec:
                return
            t_day = rec["t_day"]                               # control: another pair, same moment, no break
            for n in ctrl_draws[ev["key"]]:
                q = pairs[int(n)]
                i = idx_of[q].get(t_day)
                dq = data[q]
                if q == ev["pair"] or i is None or i < 365 or dq[i, 0] - dq[i - 365, 0] != 365 * DAY:
                    continue
                if any((q, t_day + s * DAY) in break_days for s in range(-7, 8)):
                    continue
                w = dq[i - 365:i, 2]
                j = int(np.argmax(w))
                key = "C:" + ev["key"]
                if key not in done:
                    crec = await one_event(net, {"key": key, "pair": q, "i": i, "kind": "CTRL", "old": float(w[j]),
                                                 "j": i - 365 + j, "slot": rec["slot"], "of": ev["kind"]}, dq, btc)
                    fh.write(json.dumps(crec) + "\n")
                    n_new += 1
                return

        pending: set = set()
        for n, ev in enumerate(todo):
            if time.time() - t_start > budget_s:
                break
            pending.add(asyncio.create_task(run(ev)))
            if len(pending) >= 24:
                _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if n % 200 == 0:
                fh.flush()
                print(f"  {len(done) + n_new} records, {net.requests} requests, {time.time() - t_start:.0f}s", flush=True)
        if pending:
            await asyncio.wait(pending)
        fh.close()
        left = len([e for e in events if e["key"] not in read_jsonl(out_p)])
        print(f"{n_new} new records ({net.requests} requests, {time.time() - t_start:.0f}s). "
              + ("ALL DONE" if not left else f"{left} breaks still to do - run again"), flush=True)
        return not left
    finally:
        await net.close()


async def futures(cfg, budget_s: float) -> bool:
    """Funding rate and open interest around each break (Binance USD-M perpetual with the same name)."""
    t_start = time.time()
    _, out_p, fut_p = paths(cfg)
    recs = [r for r in read_jsonl(out_p).values() if "d0" in r and r["kind"] != "CTRL" and r["t"] >= 1_577_836_800_000]
    done = read_jsonl(fut_p)
    todo = [r for r in recs if r["key"] not in done]
    print(f"{len(recs)} breaks since 2020; {len(done)} already have futures data", flush=True)
    net = Net(conc=16)
    fund_cache: dict[tuple[str, str], list | None] = {}
    fh = open(fut_p, "a", encoding="utf-8")
    n_new = 0

    async def fund_month(sym: str, month: str):
        if (sym, month) not in fund_cache:
            fund_cache[(sym, month)] = await net.vision_csv(f"monthly/fundingRate/{sym}/{sym}-fundingRate-{month}.zip")
        return fund_cache[(sym, month)]

    async def run(r: dict) -> None:
        try:
            await run_one(r)
        except Exception as e:  # noqa: BLE001
            print(f"  {r['key']}: {type(e).__name__}: {e}", flush=True)

    async def run_one(r: dict) -> None:
        nonlocal n_new
        sym, t = r["pair"], r["t"]
        out: dict = {"key": r["key"]}
        rows = []
        for month in sorted({day_str(t - 3 * DAY)[:7], day_str(t)[:7]}):   # near a month start: the previous too
            m = await fund_month(sym, month)
            if m:
                rows += [x for x in m[1:] if x and x[0].isdigit()]
        past = sorted((int(x[0]), float(x[2])) for x in rows if int(x[0]) <= t)
        if past and t - past[-1][0] <= 9 * H1:
            out["funding_pct"] = round(past[-1][1] * 100, 5)
            out["funding_3d_avg_pct"] = round(float(np.mean([v for ts, v in past if ts > t - 3 * DAY])) * 100, 5)
        if t >= 1_638_316_800_000:                              # 5-minute metrics files start in December 2021
            oi = []
            for ms in (t - DAY, t):
                m = await net.vision_csv(f"daily/metrics/{sym}/{sym}-metrics-{day_str(ms)}.zip")
                for x in (m or [])[1:]:
                    try:
                        ts = int(datetime.strptime(x[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
                        oi.append((ts, float(x[2]), float(x[3]), float(x[6].strip('"') or "nan")))
                    except (ValueError, IndexError):
                        continue
            oi = sorted(x for x in oi if x[0] <= t)
            if oi and t - oi[-1][0] <= 20 * 60_000:
                def at(ago_ms: int):
                    c = [x for x in oi if x[0] <= t - ago_ms]
                    return c[-1] if c and (t - ago_ms) - c[-1][0] <= 20 * 60_000 else None
                last = oi[-1]
                out["oi_usd"] = round(last[2])
                out["long_short_accounts"] = None if np.isnan(last[3]) else round(last[3], 3)
                for name, ago in (("1h", H1), ("4h", 4 * H1), ("24h", DAY)):
                    a = at(ago)
                    if a and a[1] > 0:
                        out[f"oi_chg_{name}_pct"] = round((last[1] / a[1] - 1) * 100, 3)
        fh.write(json.dumps(out) + "\n")
        n_new += 1

    try:
        pending: set = set()
        for n, r in enumerate(todo):
            if time.time() - t_start > budget_s:
                break
            pending.add(asyncio.create_task(run(r)))
            if len(pending) >= 32:
                _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if n % 300 == 0:
                fh.flush()
                print(f"  {len(done) + n_new} done, {net.requests} files, {time.time() - t_start:.0f}s", flush=True)
        if pending:
            await asyncio.wait(pending)
    finally:
        fh.close()
        await net.close()
    left = len(todo) - n_new
    print(f"{n_new} new ({time.time() - t_start:.0f}s). " + ("ALL DONE" if left <= 0 else f"{left} still to do - run again"),
          flush=True)
    return left <= 0


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Intraday study of 52-week / all-history high breakouts")
    ap.add_argument("stage", choices=["collect", "futures", "report"])
    ap.add_argument("--budget", type=float, default=1e9, help="stop after this many seconds (rerun to continue)")
    ap.add_argument("--limit", type=int, default=None, help="collect: only an evenly spaced sample of N breaks (testing)")
    a = ap.parse_args()
    cfg = load_config()
    if a.stage == "collect":
        asyncio.run(collect(cfg, a.budget, a.limit))
    elif a.stage == "futures":
        asyncio.run(futures(cfg, a.budget))
    else:
        from highs.breakout_report import report
        report(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
