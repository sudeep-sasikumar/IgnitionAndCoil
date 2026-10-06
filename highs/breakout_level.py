"""1-minute test of entering a breakout AT the old high with a resting buy-stop order, and of
market entries 0-15 minutes after the cross (how fast the edge decays).

    .venv\\Scripts\\python.exe -m highs.breakout_study level           (resumable; needs `collect` first)
    .venv\\Scripts\\python.exe -m highs.breakout_study level-report

For every break of highs.breakout_study: 1-minute candles from the breaking 5-minute candle
(1,000 minutes), then 5-minute candles up to 48 hours.
- The cross = the first 1-minute candle whose high is above the old high.
- Resting order: filled at the old high, or at that candle's open if it opened above it (a gap).
  Slippage on the fill is a cost set in the report, not a price change. Inside the fill candle the
  high came after the fill (price had to cross the level to get there); the LOW may have come
  before it, so two readings are stored: "pess" counts that low against the trade, "opt" ignores
  it (unless the candle gapped, where the low is certainly after the fill). The truth lies between.
- Market entries: the close of the candle 0, 1, 2, 5, 10 and 15 minutes after the cross.
Stored per break: the first minute each +X% / -X% level was reached and the 48-hour result
(<data_dir>/highs_cache/breakouts_level.jsonl). No candles are kept.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import numpy as np

from highs.breakout_report import FUNDING_PER_8H, boot_ci, month_of
from highs.breakout_study import DN, H1, M5, UP, Net, paths, read_jsonl

M1 = 60_000
AFTER = (0, 1, 2, 5, 10, 15)                 # market entries: minutes after the cross
HORIZON = 48 * H1


def touches(close_t: np.ndarray, hi: np.ndarray, lo: np.ndarray, cl: np.ndarray, base: float, t0: float) -> dict | None:
    """First minute (from t0, at the candle's close) each level was reached, and the result at 48h.
    The arrays are the candles after the entry, 1-minute first, then 5-minute."""
    m = close_t <= t0 + HORIZON
    if not m.any() or close_t[m][-1] < t0 + HORIZON - 30 * M1:
        return None                                  # trading stopped inside the 48 hours
    hi, lo, cl, mins = hi[m] / base - 1, lo[m] / base - 1, cl[m] / base - 1, (close_t[m] - t0) / M1

    def first(mask: np.ndarray) -> float:
        k = int(np.argmax(mask))
        return round(float(mins[k]), 1) if mask[k] else -1.0
    return {"up": [first(hi >= u / 100) for u in UP], "dn": [first(lo <= -x / 100) for x in DN],
            "ret48": round(float(cl[-1] * 100), 3)}


def measure_level(m1: np.ndarray, m5: np.ndarray, old: float) -> dict | None:
    """All entry variants for one break. m1 starts at the breaking 5m candle; m5 covers the 48 hours."""
    hit = np.where(m1[:, 2] > old)[0]
    if not len(hit) or hit[0] > 10:                  # the cross must be inside the breaking 5m candle (+ a little)
        return None
    k = int(hit[0])
    end1 = m1[-1, 0] + M1
    tail = m5[m5[:, 0] >= end1]
    close_t = np.concatenate([m1[:, 0] + M1, tail[:, 0] + M5])
    hi, lo, cl = (np.concatenate([m1[:, c], tail[:, c]]) for c in (2, 3, 4))
    gapped = bool(m1[k, 1] > old)
    base = float(max(old, m1[k, 1]))
    out: dict = {"cross_min": k, "gapped": gapped, "fill_vs_old_pct": round((base / old - 1) * 100, 3)}
    t0 = float(m1[k, 0])
    pess = touches(close_t[k:], hi[k:], lo[k:], cl[k:], base, t0)
    lo_opt = lo[k:].copy()
    if not gapped:
        lo_opt[0] = base                             # the fill candle's low assumed to be BEFORE the fill
    opt = touches(close_t[k:], hi[k:], lo_opt, cl[k:], base, t0)
    if not pess or not opt:
        return None
    out["pess"], out["opt"] = pess, opt
    for d in AFTER:
        j = k + d
        if j + 1 < len(m1):
            t = touches(close_t[j + 1:], hi[j + 1:], lo[j + 1:], cl[j + 1:], float(m1[j, 4]), float(m1[j, 0] + M1))
            if t:
                t["vs_old_pct"] = round((float(m1[j, 4]) / old - 1) * 100, 3)
                out[f"m{d}"] = t
    return out


async def collect_level(cfg, budget_s: float) -> bool:
    t_start = time.time()
    _, ev_p, _ = paths(cfg)
    out_p = ev_p.with_name("breakouts_level.jsonl")
    recs = [r for r in read_jsonl(ev_p).values() if r["kind"] != "CTRL" and "d0" in r]
    done = read_jsonl(out_p)
    todo = [r for r in recs if r["key"] not in done]
    print(f"{len(recs)} breaks; {len(done)} already measured at 1 minute", flush=True)
    net = Net()
    n_new = 0
    fh = open(out_p, "a", encoding="utf-8")

    async def run(r: dict) -> None:
        nonlocal n_new
        try:
            start = r["t"] - M5                          # the breaking 5-minute candle's open
            m1, m5 = await asyncio.gather(net.klines(r["pair"], "1m", start), net.klines(r["pair"], "5m", start))
            res = measure_level(m1, m5, r["old"]) if len(m1) and len(m5) else None
            fh.write(json.dumps({"key": r["key"], "pair": r["pair"], "kind": r["kind"], "t": r["t"],
                                 **(res or {"skip": "no clean 1-minute path"})}) + "\n")
            n_new += 1
        except Exception as e:  # noqa: BLE001 - retried on the next run
            print(f"  {r['key']}: {type(e).__name__}: {e}", flush=True)

    try:
        pending: set = set()
        for n, r in enumerate(todo):
            if time.time() - t_start > budget_s:
                break
            pending.add(asyncio.create_task(run(r)))
            if len(pending) >= 24:
                _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if n % 300 == 0:
                fh.flush()
                print(f"  {len(done) + n_new} done, {net.requests} requests, {time.time() - t_start:.0f}s", flush=True)
        if pending:
            await asyncio.wait(pending)
    finally:
        fh.close()
        await net.close()
    left = len(todo) - n_new
    print(f"{n_new} new ({time.time() - t_start:.0f}s). " + ("ALL DONE" if left <= 0 else f"{left} still to do - run again"),
          flush=True)
    return left <= 0


# ---- report ----------------------------------------------------------------------------------------

def race(recs: list[dict], variant: str, tp: float, sl: float, entry_cost: float, exit_costs: tuple[float, float]):
    """(net % of the position, code 1 target / -1 stop / 0 timed out). exit_costs = (target fill, market exit)."""
    up = np.array([r[variant]["up"][UP.index(tp)] for r in recs])
    dn = np.array([r[variant]["dn"][DN.index(sl)] for r in recs])
    final = np.array([r[variant]["ret48"] for r in recs])
    stop = (dn >= 0) & ((up < 0) | (dn <= up))           # reached in the same candle: the stop is assumed
    win = (up >= 0) & ~stop
    held_h = np.where(stop, dn, np.where(win, up, 48 * 60)) / 60
    res = np.where(stop, -sl - exit_costs[1], np.where(win, tp - exit_costs[0], final - exit_costs[1]))
    return res - entry_cost - FUNDING_PER_8H * held_h / 8, np.where(stop, -1, np.where(win, 1, 0)), up


def level_report(cfg) -> dict:
    _, ev_p, _ = paths(cfg)
    allr = sorted((r for r in read_jsonl(ev_p.with_name("breakouts_level.jsonl")).values()), key=lambda r: r["t"])
    ok = [r for r in allr if "pess" in r]
    f = cfg.trade
    taker, maker, slip = float(f.taker_fee) * 100, float(f.maker_fee) * 100, float(f.slippage_pct)
    exits = (maker, taker + slip)                        # target = limit order; stop / time exit = market order
    L: list[str] = []
    P = L.append
    months = np.array([month_of(r["t"]) for r in ok])
    t = np.array([r["t"] for r in ok], float)
    half = float(np.median(t))
    cells = ((3, 3), (5, 3), (5, 2), (10, 4), (10, 2), (20, 4))
    out: dict = {"generated_ms": int(datetime.now(timezone.utc).timestamp() * 1000), "n": len(ok), "rows": {}}
    P("=" * 150)
    P(f"ENTERING AT THE OLD HIGH - 1-MINUTE DATA   {len(ok):,} breaks measured, {len(allr) - len(ok)} without a clean 1-minute path")
    gap = np.array([r["fill_vs_old_pct"] for r in ok])
    P(f"  the candle that crossed opened above the old high (a gap) in {np.mean([r['gapped'] for r in ok]) * 100:.0f}% of breaks; "
      f"fill vs the old high: median {np.median(gap):+.2f}%, 90% of breaks under {np.percentile(gap, 90):+.2f}%")
    P(f"  costs, % of the position: market order {taker + slip:.2f} (resting order: {taker:.2f} + the stated slippage), target {maker:.2f}; "
      f"funding {FUNDING_PER_8H}% per 8h. Held at most 48 hours. x20 = % of the margin at 20x.")
    P("  each cell: average result [90% range] target hit %")
    P("")
    P(f"  {'entry':46}" + "".join(f"{'+' + str(a) + ' / -' + str(b):^27}" for a, b in cells))

    def line(label: str, variant: str, entry_cost: float, mask: np.ndarray | None = None) -> None:
        rs = [r for i, r in enumerate(ok) if variant in r and (mask is None or mask[i])]
        mm = np.array([month_of(r["t"]) for r in rs])
        txt, rec = [], {"n": len(rs)}
        for tp, sl in cells:
            res, code, _ = race(rs, variant, tp, sl, entry_cost, exits)
            lo, hi = boot_ci(res, mm)
            rec[f"{tp}/{sl}"] = {"avg": float(res.mean()), "lo": lo, "hi": hi, "hit": float((code == 1).mean())}
            txt.append(f"{res.mean():+5.2f} [{lo:+5.2f}..{hi:+5.2f}] {100 * (code == 1).mean():3.0f}%")
        out["rows"][label] = rec
        P(f"  {label:38} n={len(rs):5,} " + " | ".join(txt))

    P("  RESTING BUY-STOP ORDER AT THE OLD HIGH, 0.10% slippage on the fill")
    for name, lab in (("opt", "best case (fill candle's low was before the fill)"), ("pess", "worst case (that low came after the fill)")):
        line(lab[:38], name, taker + 0.10)
        line("   until 2023-12", name, taker + 0.10, t <= half)
        line("   since 2023-12", name, taker + 0.10, t > half)
    P("  the same order with more slippage on the fill (worst case .. best case shown as two lines each)")
    for s in (0.05, 0.2, 0.3, 0.5):
        line(f"   slippage {s:.2f}%  best case", "opt", taker + s)
        line(f"   slippage {s:.2f}%  worst case", "pess", taker + s)
    P("")
    P("  MARKET ORDER AFTER THE CROSS (the close of the 1-minute candle N minutes later)")
    for d in AFTER:
        vs = np.array([r[f"m{d}"]["vs_old_pct"] for r in ok if f"m{d}" in r])
        line(f"{d:2d} min later (median {np.median(vs):+.2f}% above old high)", f"m{d}", taker + slip)
    P("")
    P("  WHERE THE RESULT COMES FROM (+10 / -4, resting order, 0.10% slippage)")
    for name in ("opt", "pess"):
        res, code, up = race(ok, name, 10, 4, taker + 0.10, exits)
        w = code == 1
        P(f"   {name}: {w.mean() * 100:.0f}% hit the target, {np.mean(code == -1) * 100:.0f}% stopped, {np.mean(code == 0) * 100:.0f}% timed out; "
          f"target reached within 5 min in {np.mean(up[w] <= 5) * 100:.0f}% of the winners, within 15 min {np.mean(up[w] <= 15) * 100:.0f}%, "
          f"within 1h {np.mean(up[w] <= 60) * 100:.0f}%")
        P(f"        average {res.mean():+.2f}; counting winners that got there within 5 min as zero: {np.where(w & (up <= 5), 0, res).mean():+.2f}; "
          f"within 15 min: {np.where(w & (up <= 15), 0, res).mean():+.2f}")
        dn4 = np.array([r[name]["dn"][DN.index(4)] for r in ok])
        s_ = code == -1
        P(f"        stopped within 1 min of the fill: {np.mean(dn4[s_] <= 1) * 100:.0f}% of the stops, within 5 min {np.mean(dn4[s_] <= 5) * 100:.0f}%, "
          f"within 1h {np.mean(dn4[s_] <= 60) * 100:.0f}%")
        years = np.array([datetime.fromtimestamp(x / 1000, tz=timezone.utc).year for x in t])
        P("        by year: " + "  ".join(f"{y}: {res[years == y].mean():+.2f} (n={np.sum(years == y)})" for y in sorted(set(years)) if np.sum(years == y) >= 10))
    # ---- by group (things known before the break, so usable for a resting order)
    feats = {k: r.get("f", {}) for k, r in read_jsonl(ev_p).items()}
    fa = lambda name: np.array([feats.get(r["key"], {}).get(name, np.nan) for r in ok], float)  # noqa: E731
    kinds = np.array([r["kind"] for r in ok])
    liq, age, near, atr, btc = fa("liq_usd_30d"), fa("age_days"), fa("near_days_30"), fa("atr_pct_14d"), fa("btc_above_200d")
    P("")
    P("  BY GROUP (+10 / -4, resting order, 0.10% slippage): worst case .. best case, [90% range of the worst case], target hit % worst..best")
    rp, cp, _ = race(ok, "pess", 10, 4, taker + 0.10, exits)
    ro, co, _ = race(ok, "opt", 10, 4, taker + 0.10, exits)
    out["groups"] = {}
    for label, m in (("all-history high", kinds == "AH"), ("52-week high", kinds == "52W"),
                     ("Bitcoin above its 200-day average", btc == 1), ("Bitcoin below its 200-day average", btc == 0),
                     ("daily volume under $5M", liq < 5e6), ("daily volume $5M-$50M", (liq >= 5e6) & (liq < 5e7)),
                     ("daily volume over $50M", liq >= 5e7),
                     ("old high 7-30 days old", age <= 30), ("old high 31-180 days old", (age > 30) & (age <= 180)),
                     ("old high older than 180 days", age > 180),
                     ("0-1 days near the high in the last 30", near <= 1), ("2-4 days near the high", (near >= 2) & (near <= 4)),
                     ("5+ days near the high", near >= 5),
                     ("calm coin (daily range under 8%)", atr < 8), ("daily range 8-12%", (atr >= 8) & (atr < 12)),
                     ("wild coin (daily range over 12%)", atr >= 12)):
        if m.sum() < 30:
            continue
        lo, hi = boot_ci(rp[m], months[m])
        out["groups"][label] = {"n": int(m.sum()), "worst": float(rp[m].mean()), "best": float(ro[m].mean()), "lo": lo, "hi": hi}
        P(f"   {label:40} n={m.sum():5,}  {rp[m].mean():+5.2f} .. {ro[m].mean():+5.2f}  [{lo:+5.2f}..{hi:+5.2f}]  "
          f"{100 * (cp[m] == 1).mean():3.0f}%..{100 * (co[m] == 1).mean():3.0f}%")

    # ---- as a plan: every break, a $1,000 position
    P("")
    P("  AS A PLAN: every break taken with a $1,000 position ($50 margin at 20x), +10 / -4, in date order")
    out["plan"] = {}
    for name, res, code in (("worst case", rp, cp), ("best case", ro, co)):
        usd = res * 10                                    # % of $1,000
        eq = np.cumsum(usd)
        dd = float((np.maximum.accumulate(eq) - eq).max())
        streak = longest = 0
        for c in code:
            streak = streak + 1 if c != 1 else 0
            longest = max(longest, streak)
        yrs = (t[-1] - t[0]) / (365 * 86_400_000)
        mm_ = np.unique(months)
        monthly = np.array([usd[months == x].sum() for x in mm_])
        out["plan"][name] = {"total_usd": float(eq[-1]), "max_drawdown_usd": dd, "longest_losing_run": int(longest),
                             "losing_months": int((monthly < 0).sum()), "months": int(len(mm_))}
        P(f"   {name}: total ${eq[-1]:+,.0f} over {yrs:.1f} years ({len(res):,} trades; a win ${usd[code == 1].mean():+.0f}, a stop ${usd[code == -1].mean():+.0f}); "
          f"deepest drawdown ${dd:,.0f}; longest run without a win {longest} trades; "
          f"{(monthly < 0).sum()} losing months of {len(mm_)} with trades (worst ${monthly.min():+,.0f}, best ${monthly.max():+,.0f})")
    busiest = max(set(months), key=lambda x: np.sum(months == x))
    P(f"   trades are bunched: {np.sum(months == busiest)} of them in {busiest} alone; {np.mean(np.diff(t) < 3_600_000) * 100:.0f}% start within an hour of the previous one")
    text = "\n".join(L)
    print(text)
    from pathlib import Path
    base = Path(cfg.data_dir)
    (base / "highs_breakout_level_report.txt").write_text(text, encoding="utf-8")
    (base / "highs_breakout_level_report.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\nSaved: {base / 'highs_breakout_level_report.txt'}")
    return out
