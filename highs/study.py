"""Event study: what happened after coins broke a 52-week high or an all-time high?

    .venv\\Scripts\\python.exe -m highs.study            (a few minutes; writes var\\highs_study.json)

Coins: today's CoinGecko top N (highs.top_n), minus stablecoins and wrapped / staked copies.
History: Binance spot daily candles (<COIN>USDT; free public market data, full history since
listing). CoinGecko's free API only serves 365 days, too short to know the high a year BEFORE
each break.

Events (daily, no lookahead - day i only uses days before it):
- ATH break: close[i] > the highest high of all earlier days, and that high is at least
  highs.min_high_age_days old. Only for coins whose Binance history contains their true ATH
  (CoinGecko's ATH within 10% of Binance's highest high) - otherwise "ATH on Binance" would
  just be the highest price since Binance listed it.
- 52W break: close[i] > the highest high of the previous 365 days, the same age rule, and
  NOT an ATH break. Needs >= 365 days of history before the break.
Entry = the breaking day's close. Returns close-to-close at +1..+90 days; max rise / drop =
highest high / lowest low within 30 days; "held 30d" = close[i+30] above the old high;
"failed <=3d" = a close back below the old high within 3 days; "vs BTC" = return minus BTC's.
A baseline row does the same from EVERY day of the same coins, for comparison.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_config  # noqa: E402
from highs.coingecko import CoinGecko  # noqa: E402
from highs.detector import excluded  # noqa: E402

log = logging.getLogger("highs.study")
BINANCE = "https://data-api.binance.vision/api/v3"      # Binance's public market-data-only host
DAY = 86_400_000
HORIZONS = [1, 3, 7, 14, 30, 60, 90]


# ---- data ------------------------------------------------------------------------------------

async def binance_pairs(client: httpx.AsyncClient) -> dict[str, str]:
    """base asset -> <BASE>USDT for USDT spot pairs that are trading."""
    r = await client.get(f"{BINANCE}/exchangeInfo", params={"permissions": "SPOT"})
    r.raise_for_status()
    return {s["baseAsset"].upper(): s["symbol"] for s in r.json()["symbols"]
            if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}


async def daily(client: httpx.AsyncClient, pair: str, cache: Path, now_ms: int) -> np.ndarray:
    """Daily candles [t, o, h, l, c] (closed days only), cached and topped up."""
    p = cache / f"{pair}_1d.npy"
    have = np.load(p) if p.exists() else np.empty((0, 5))
    start = int(have[-1, 0]) + DAY if len(have) else 0
    rows = []
    while True:
        r = await client.get(f"{BINANCE}/klines", params={"symbol": pair, "interval": "1d", "startTime": start,
                                                          "limit": 1000})
        if r.status_code == 429:
            await asyncio.sleep(float(r.headers.get("retry-after") or 30))
            continue
        r.raise_for_status()
        got = r.json()
        rows += [[float(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in got if int(k[6]) < now_ms]
        if len(got) < 1000:
            break
        start = int(got[-1][0]) + DAY
    if rows:
        have = np.vstack([have, np.array(rows)]) if len(have) else np.array(rows)
        np.save(p, have)
    return have


# ---- events --------------------------------------------------------------------------------------

def find_events(d: np.ndarray, min_age: int, ath_valid: bool) -> list[tuple[int, str, float, int]]:
    """(day index, kind, old high, index of the old high) for every break (see module doc)."""
    h, c = d[:, 2], d[:, 4]
    out = []
    run_max, run_idx = -np.inf, -1
    for i in range(len(d)):
        if i >= 365 and i > 0:
            lo = i - 365
            w = h[lo:i]
            j = lo + int(np.argmax(w))
            hi52 = h[j]
            if ath_valid and c[i] > run_max and i - run_idx >= min_age:
                out.append((i, "ATH", float(run_max), run_idx))
            elif c[i] > hi52 and i - j >= min_age and (c[i] <= run_max or not ath_valid):
                out.append((i, "52W", float(hi52), j))
        if h[i] > run_max:
            run_max, run_idx = h[i], i
    return out


def outcome(d: np.ndarray, i: int, old_high: float, btc: dict[int, float]) -> dict:
    t, h, l, c = d[:, 0], d[:, 2], d[:, 3], d[:, 4]
    e = c[i]
    res: dict = {"ret": {}, "exc": {}}
    for k in HORIZONS:
        if i + k < len(d):
            res["ret"][k] = (c[i + k] / e - 1) * 100
            b0, b1 = btc.get(int(t[i])), btc.get(int(t[i + k]))
            if b0 and b1:
                res["exc"][k] = res["ret"][k] - (b1 / b0 - 1) * 100
    if i + 30 < len(d):
        res["runup30"] = (h[i + 1:i + 31].max() / e - 1) * 100
        res["dd30"] = (l[i + 1:i + 31].min() / e - 1) * 100
        res["held30"] = bool(c[i + 30] > old_high)
    if i + 3 < len(d):
        res["failed3"] = bool((c[i + 1:i + 4] < old_high).any())
    return res


def summarise(label: str, items: list[dict]) -> dict:
    g: dict = {"label": label, "n": len(items), "ret": {}}
    for k in HORIZONS:
        x = np.array([it["ret"][k] for it in items if k in it["ret"]])
        if len(x):
            g["ret"][k] = {"n": int(len(x)), "mean": float(x.mean()), "median": float(np.median(x)),
                           "win": float((x > 0).mean())}
    ex = np.array([it["exc"][30] for it in items if 30 in it["exc"]])
    g["excess30"] = float(ex.mean()) if len(ex) else None
    for key, name in (("runup30", "runup30_med"), ("dd30", "dd30_med")):
        x = [it[key] for it in items if key in it]
        g[name] = float(np.median(x)) if x else None
    for key in ("held30", "failed3"):
        x = [it[key] for it in items if key in it]
        g[key] = float(np.mean(x)) if x else None
    return g


def rank_bucket(rank: int | None) -> str:
    if not rank:
        return "rank ?"
    return "rank 1-100" if rank <= 100 else "rank 101-500" if rank <= 500 else "rank 501+"


# ---- main ------------------------------------------------------------------------------------------

async def run(cfg, top_n: int) -> dict:
    h = cfg.highs
    now = int(time.time() * 1000)
    cache = Path(cfg.data_dir) / "highs_cache"
    cache.mkdir(parents=True, exist_ok=True)
    cg = CoinGecko(h.api_base, h.calls_per_min)
    try:
        print(f"CoinGecko: top {top_n} coins...", flush=True)
        coins = await cg.top(top_n)
    finally:
        await cg.close()
    stables = {s.upper() for s in cfg.universe.stablecoin_bases}
    seen: set[str] = set()
    picked = []
    for r in coins:
        sym = str(r.get("symbol") or "").upper()
        if not sym or sym in seen or excluded(str(r.get("name") or ""), sym, stables, list(h.exclude_name_keywords)):
            continue
        seen.add(sym)            # the same ticker twice: keep the higher-ranked coin
        picked.append(r)
    async with httpx.AsyncClient(timeout=30) as client:
        pairs = await binance_pairs(client)
        matched = [(r, pairs[str(r["symbol"]).upper()]) for r in picked if str(r["symbol"]).upper() in pairs]
        print(f"Binance: {len(matched)} of {len(picked)} coins have a USDT spot pair; loading daily history...", flush=True)
        sem = asyncio.Semaphore(8)
        data: dict[str, np.ndarray] = {}

        async def one(pair: str) -> None:
            async with sem:
                try:
                    data[pair] = await daily(client, pair, cache, now)
                except httpx.HTTPError as e:
                    log.warning("%s: %s", pair, e)
        await asyncio.gather(*(one(p) for p in {p for _, p in matched} | {"BTCUSDT"}))

    btc_d = data["BTCUSDT"]
    btc = {int(t): float(c) for t, c in zip(btc_d[:, 0], btc_d[:, 4])}
    btc_sma = {}
    bc = btc_d[:, 4]
    for i in range(200, len(btc_d)):
        btc_sma[int(btc_d[i, 0])] = bc[i] > bc[i - 200:i].mean()
    min_age = int(h.min_high_age_days)
    events: list[dict] = []
    base: list[dict] = []
    n_hist = n_ath_valid = 0
    for r, pair in matched:
        d = data.get(pair)
        if d is None or len(d) < 400:
            continue
        n_hist += 1
        cg_ath = r.get("ath")
        ath_valid = bool(cg_ath) and cg_ath <= d[:, 2].max() * 1.10
        n_ath_valid += ath_valid
        for i, kind, old, j in find_events(d, min_age, ath_valid):
            o = outcome(d, i, old, btc)
            o.update(kind=kind, rank=r.get("market_cap_rank"), t=int(d[i, 0]), btc_up=btc_sma.get(int(d[i, 0])))
            events.append(o)
        for i in range(365, len(d) - 1, 7):     # baseline: every 7th day from the same coins
            o = outcome(d, i, float("inf"), btc)
            o.pop("held30", None)
            o.pop("failed3", None)
            base.append(o)

    def groups(pred_label):
        out = defaultdict(list)
        for e in events:
            lab = pred_label(e)
            if lab:
                out[lab].append(e)
        return [summarise(k, v) for k, v in sorted(out.items())]

    overall = [summarise("All-time high breaks", [e for e in events if e["kind"] == "ATH"]),
               summarise("52-week high breaks (not ATH)", [e for e in events if e["kind"] == "52W"]),
               summarise("Baseline: any day, same coins", base)]
    year = lambda e: datetime.fromtimestamp(e["t"] / 1000, tz=timezone.utc).year  # noqa: E731
    res = {
        "generated_ms": now, "horizons": HORIZONS,
        "params": {"top_n": top_n, "min_high_age_days": min_age},
        "coverage": {"cg_top": len(coins), "after_exclusions": len(picked), "matched": len(matched),
                     "coins_with_history": n_hist, "coins_with_true_ath_on_binance": int(n_ath_valid),
                     "events_ath": sum(e["kind"] == "ATH" for e in events),
                     "events_52w": sum(e["kind"] == "52W" for e in events)},
        "overall": overall,
        "by_rank": groups(lambda e: f"{e['kind']} · {rank_bucket(e['rank'])}"),
        "by_btc": groups(lambda e: None if e["btc_up"] is None else
                         f"{e['kind']} · BTC {'above' if e['btc_up'] else 'below'} its 200-day average"),
        "by_year": groups(lambda e: f"{e['kind']} · {year(e)}"),
        "notes": [
            "History: Binance spot daily candles (<COIN>USDT) - free public market data. CoinGecko's free API "
            "only serves 365 days of history, which is too short for this study.",
            "Coins = today's CoinGecko top list: coins that crashed out of it or were delisted are missing "
            "(survivorship bias - results look better than reality). Rank buckets use today's rank.",
            "ATH breaks only for coins whose Binance history contains their real ATH (CoinGecko ATH within 10% "
            "of Binance's highest high); other coins' 'ATH on Binance' would only be a post-listing high.",
            f"A break needs the old high to be at least {min_age} days old, so a trend making new highs every "
            "day counts once. Entry is the breaking day's close (a live alert fires intraday, earlier).",
            "Returns are before fees, slippage and funding; no leverage. Past behaviour, not a promise.",
        ],
    }
    ath, w52, b = overall
    res["headline"] = [headline(ath, b), headline(w52, b)]
    return res


def headline(g: dict, base: dict) -> str:
    r30, b30 = g["ret"].get(30), base["ret"].get(30)
    if not r30 or not b30:
        return f"{g['label']}: not enough events."
    return (f"{g['label']} ({g['n']:,} events): after 30 days the median coin was {r30['median']:+.1f}% "
            f"(average {r30['mean']:+.1f}%, {r30['win'] * 100:.0f}% higher) vs {b30['median']:+.1f}% median on "
            f"any day; {g['held30'] * 100:.0f}% still traded above the old high, "
            f"{g['failed3'] * 100:.0f}% fell back below it within 3 days.")


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_config()
    ap = argparse.ArgumentParser(description="What happened after 52-week / all-time high breaks")
    ap.add_argument("--top", type=int, default=int(cfg.highs.top_n))
    a = ap.parse_args()
    res = asyncio.run(run(cfg, a.top))
    out = Path(cfg.data_dir) / cfg.highs.study_file
    out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    c = res["coverage"]
    print(f"{c['coins_with_history']} coins with >=1 year of history; {c['events_ath']} ATH and {c['events_52w']} 52W breaks.")
    for line in res["headline"]:
        print(line)
    print(f"Saved: {out}  (shown on the dashboard's Highs tab)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
