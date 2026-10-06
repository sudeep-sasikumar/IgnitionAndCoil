"""Report for highs.breakout_study: reads the stored per-event results and prints / saves the tables.

Trade maths (per $ of position, so x leverage for the margin): a target is a limit order (maker
fee), everything else is a market order (taker fee + slippage), fees from config.yaml; funding is
charged at FUNDING_PER_8H for the time held. If a target and a stop are first reached in the same
candle, the STOP is assumed (conservative). Uncertainty: 90% range from resampling whole calendar
months (breakouts cluster in time, so single events are not independent). "Early" / "late" = the
first and second half of the events by date: a finding counts only if it shows in both.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from highs.breakout_study import DAY, DN, UP, paths, read_jsonl

FUNDING_PER_8H = 0.01            # % of the position per 8h held (the usual base rate; see the futures table)


def month_of(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m")


def boot_ci(x: np.ndarray, months: np.ndarray, n: int = 400, seed: int = 3) -> tuple[float, float]:
    """90% range of the mean, resampling calendar months."""
    if len(x) < 20:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    um, inv = np.unique(months, return_inverse=True)
    sums = np.bincount(inv, weights=x, minlength=len(um))
    cnts = np.bincount(inv, minlength=len(um))
    pick = rng.integers(0, len(um), size=(n, len(um)))
    means = sums[pick].sum(1) / np.maximum(cnts[pick].sum(1), 1)
    return (float(np.percentile(means, 5)), float(np.percentile(means, 95)))


class Set:
    """Events (or controls) entered with one delay, as arrays."""

    def __init__(self, recs: list[dict], delay: str, costs: dict):
        self.recs = [r for r in recs if delay in r]
        self.m = [r[delay] for r in self.recs]
        self.n = len(self.recs)
        self.t = np.array([r["t"] for r in self.recs], float)
        self.months = np.array([month_of(r["t"]) for r in self.recs])
        self.costs = costs

    def col(self, name: str) -> np.ndarray:
        return np.array([m.get(name, np.nan) for m in self.m], float)

    def touch(self, name: str) -> np.ndarray:
        """[events x levels] first-touch times; -1 = never; NaN row = horizon not available."""
        width = len(UP) if name.startswith("up") else len(DN)
        return np.array([m.get(name, [np.nan] * width) for m in self.m], float)

    def race(self, tp: float, sl: float, horizon: str = "48") -> tuple[np.ndarray, np.ndarray]:
        """(net result in % of the position, outcome code 1 target / -1 stop / 0 timed out) per event."""
        up, dn = self.touch("up" + horizon)[:, UP.index(tp)], self.touch("dn" + horizon)[:, DN.index(sl)]
        final = self.col("ret_48h" if horizon == "48" else "ret_30d")
        hours_total = 48.0 if horizon == "48" else 720.0
        to_h = (lambda a: (a + 1) * 5 / 60) if horizon == "48" else (lambda a: a)
        c = self.costs
        stop = (dn >= 0) & ((up < 0) | (dn <= up))
        win = (up >= 0) & ~stop
        held = np.where(stop, to_h(dn), np.where(win, to_h(up), hours_total))
        fund = FUNDING_PER_8H * held / 8
        res = np.where(stop, -sl - c["loss"], np.where(win, tp - c["win"], final - c["loss"])) - fund
        res[np.isnan(up) | np.isnan(final)] = np.nan
        return res, np.where(stop, -1, np.where(win, 1, 0))


def share(mask: np.ndarray) -> str:
    return f"{np.mean(mask) * 100:4.0f}%" if len(mask) else "  – "


def cell(res: np.ndarray, code: np.ndarray, months: np.ndarray, m: np.ndarray) -> tuple[str, dict | None]:
    if m.sum() < 20:
        return "          –            ", None
    lo, hi = boot_ci(res[m], months[m])
    d = {"n": int(m.sum()), "avg": float(res[m].mean()), "lo": lo, "hi": hi,
         "target_hit": float((code[m] == 1).mean()), "stopped": float((code[m] == -1).mean())}
    return f"{d['avg']:+5.2f} [{lo:+5.2f}..{hi:+5.2f}] {100 * d['target_hit']:3.0f}%", d


def report(cfg) -> dict:
    _, ev_p, fut_p = paths(cfg)
    allr = list(read_jsonl(ev_p).values())
    fut = read_jsonl(fut_p)
    f = cfg.trade
    taker, maker, slip = float(f.taker_fee) * 100, float(f.maker_fee) * 100, float(f.slippage_pct)
    costs = {"win": taker + slip + maker, "loss": 2 * (taker + slip)}
    ev = [r for r in allr if r["kind"] != "CTRL"]
    ct = [r for r in allr if r["kind"] == "CTRL"]
    out: dict = {"generated_ms": int(datetime.now(timezone.utc).timestamp() * 1000), "costs_pct": costs,
                 "funding_per_8h_pct": FUNDING_PER_8H}
    L: list[str] = []
    P = L.append

    ok = sorted((r for r in ev if "d0" in r), key=lambda r: r["t"])
    cok = [r for r in ct if "d0" in r]
    skips: dict[str, int] = {}
    for r in ev:
        if "skip" in r:
            skips[r["skip"]] = skips.get(r["skip"], 0) + 1
    P("=" * 110)
    P("SAMPLE")
    P(f"  {len(ok):,} breaks measured ({sum(r['kind'] == 'AH' for r in ok):,} all-history highs, "
      f"{sum(r['kind'] == '52W' for r in ok):,} 52-week highs) on {len({r['pair'] for r in ok})} pairs, "
      f"{month_of(min(r['t'] for r in ok))} to {month_of(max(r['t'] for r in ok))}")
    P(f"  {len(cok):,} controls (another coin at the same moment, not breaking)   not measured: {skips or 'none'}")
    P(f"  costs per trade, % of the position: {costs['win']:.2f} when the target fills, {costs['loss']:.2f} otherwise; "
      f"funding {FUNDING_PER_8H}% per 8h held. At 20x multiply every % by 20 for the margin.")
    out["sample"] = {"breaks": len(ok), "ah": sum(r["kind"] == "AH" for r in ok), "w52": sum(r["kind"] == "52W" for r in ok),
                     "controls": len(cok), "pairs": len({r["pair"] for r in ok}), "skipped": skips}

    groups = {"All breaks": ok, "All-history high": [r for r in ok if r["kind"] == "AH"],
              "52-week high": [r for r in ok if r["kind"] == "52W"], "Control": cok}
    sets = {g: Set(rs, "d0", costs) for g, rs in groups.items()}
    s, sc = sets["All breaks"], sets["Control"]
    half = float(np.median(s.t))

    # ---- A. how far, how deep
    P("")
    P("A. HOW FAR IT RUNS AFTER THE ALERT, AND HOW DEEP IT DIPS (from the entry price, %; median [25%..75%])")
    out["excursions"] = {}
    for g, ss in sets.items():
        P(f"  {g} (n={ss.n:,})")
        row = {}
        for hz in ("1h", "4h", "24h", "48h", "30d"):
            up, dn, rt = ss.col(f"mfe_{hz}"), ss.col(f"mae_{hz}"), ss.col(f"ret_{hz}")
            v = np.isfinite(up)
            if not v.any():
                continue
            u, d, r = (np.percentile(a[v], [25, 50, 75]) for a in (up, dn, rt))
            row[hz] = {"n": int(v.sum()), "best": u.tolist(), "worst": d.tolist(), "end": r.tolist()}
            P(f"    within {hz:>3}: best {u[1]:+6.1f} [{u[0]:+5.1f}..{u[2]:+6.1f}]   worst {d[1]:+6.1f} [{d[0]:+6.1f}..{d[2]:+5.1f}]"
              f"   at the end {r[1]:+6.1f} [{r[0]:+6.1f}..{r[2]:+6.1f}]")
        out["excursions"][g] = row

    # ---- B. reach rates
    P("")
    P("B. SHARE THAT REACHED +X% (and -X%) FROM THE ENTRY AT SOME POINT")
    out["reach"] = {}
    for hz, key_u, key_d in (("48 hours", "up48", "dn48"), ("30 days", "up30", "dn30")):
        P(f"  within {hz}:        " + "".join(f"{'+' + str(u) + '%':>7}" for u in UP) + "   |" + "".join(f"{'-' + str(x) + '%':>7}" for x in DN[:8]))
        for g, ss in sets.items():
            u, d = ss.touch(key_u), ss.touch(key_d)
            v = np.isfinite(u[:, 0])
            ru, rd = (u[v] >= 0).mean(0) * 100, (d[v] >= 0).mean(0) * 100
            out["reach"][f"{g}|{hz}"] = {"n": int(v.sum()), "up": dict(zip(map(str, UP), ru.round(1).tolist())),
                                         "down": dict(zip(map(str, DN), rd.round(1).tolist()))}
            P(f"    {g:18}" + "".join(f"{x:6.0f}%" for x in ru) + "   |" + "".join(f"{x:6.0f}%" for x in rd[:8]))

    # ---- C. time to the peak
    P("")
    P("C. WHEN THE PEAK CAME")
    tp48 = (s.col("t_peak48") + 1) * 5 / 60
    P("  peak of the first 48 hours: " + ", ".join(f"within {h}h {share(tp48 <= h)}" for h in (0.25, 1, 4, 12, 24)) +
      f"; median {np.median(tp48):.1f}h")
    dip = s.col("dip_before_peak48")
    P(f"  dip suffered BEFORE that peak: median {np.median(dip):+.1f}%, a quarter worse than {np.percentile(dip, 25):+.1f}%, "
      f"one in ten worse than {np.percentile(dip, 10):+.1f}%")
    tp30 = s.col("t_peak30_h")
    v = np.isfinite(tp30)
    P("  peak of the 30 days: " + ", ".join(f"within {d}d {share(tp30[v] <= d * 24)}" for d in (1, 2, 7, 14, 21)) +
      f"; median day {np.median(tp30[v]) / 24:.1f}")
    out["peak_time"] = {"median_h_48": float(np.median(tp48)), "median_d_30": float(np.median(tp30[v]) / 24),
                        "dip_before_peak_q": np.percentile(dip, [10, 25, 50]).tolist()}

    # ---- D. failed breakouts
    P("")
    P("D. FALLING BACK UNDER THE OLD HIGH (a 5-minute close below it)")
    b = s.col("below_old48")
    hrs = np.where(b >= 0, (b + 1) * 5 / 60, np.inf)
    P("  " + ", ".join(f"within {h}h {share(hrs <= h)}" for h in (0.25, 1, 4, 24, 48)) + f"; never in 48h {share(b < 0)}")
    for name in ("above_old_24h", "above_old_7d", "above_old_30d"):
        x = s.col(name)
        P(f"  still above the old high after {name.split('_')[-1]}: {share(x[np.isfinite(x)] > 0)}")
    out["fail"] = {"back_below_1h": float((hrs <= 1).mean()), "back_below_24h": float((hrs <= 24).mean()),
                   "never_48h": float((b < 0).mean())}

    # ---- E. exits: target vs stop
    P("")
    P("E. TARGET vs STOP, FIRST TOUCHED WINS (net of costs, % of the position; x20 = % of the margin at 20x)")
    P("   columns: all breaks | early half | late half | control.   Each: average result, [90% range], target hit %")
    out["exits"] = []

    def grid(horizon: str, tps: list, sls: list) -> None:
        for tp in tps:
            for sl in sls:
                cells, rec = [], {"horizon": horizon, "tp": tp, "sl": sl}
                for g, ss, mf in (("all", s, None), ("early", s, lambda t: t <= half), ("late", s, lambda t: t > half),
                                  ("control", sc, None)):
                    res, code = ss.race(tp, sl, horizon)
                    m = np.isfinite(res) if mf is None else np.isfinite(res) & mf(ss.t)
                    txt, rec[g] = cell(res, code, ss.months, m)
                    cells.append(txt)
                out["exits"].append(rec)
                P(f"   +{tp:<3}/ -{sl:<3} " + " | ".join(cells))
    P("  held at most 48 hours (5-minute data); a 20x position is liquidated near -4.8%, so the stop must be tighter:")
    grid("48", [2, 3, 5, 10, 20], [1, 2, 3, 4])
    P("  held up to 30 days (bigger targets need wider stops, so LOWER leverage: a -10% stop allows about 8x):")
    grid("30", [10, 20, 30, 50, 100], [5, 10, 15, 20])

    # ---- F. entering later / confirmation
    P("")
    P("F. DOES IT MATTER WHEN YOU ENTER? (held at most 48h from the entry; average result [90% range] target hit %)")
    P(f"  {'':52}{'+3 / -3':^26}|{'+5 / -3':^26}|{'+10 / -4':^26}")
    out["delay"] = {}
    CELLS = ((3, 3), (5, 3), (10, 4))

    def delay_line(label: str, rs: list[dict], name: str) -> None:
        ss = Set(rs, name, costs)
        txts, rec = [], {"n": ss.n}
        for tp, sl in CELLS:
            res, code = ss.race(tp, sl)
            txt, rec[f"{tp}/{sl}"] = cell(res, code, ss.months, np.isfinite(res))
            txts.append(txt)
        out["delay"][label] = rec
        P(f"  {label:44} n={ss.n:5,} " + " | ".join(txts))
    for name, label in (("lvl", "buy-stop order resting at the old high"), ("d0", "at the alert (within 5 minutes)"),
                        ("d15", "15 minutes later"), ("d60", "1 hour later"), ("d4h", "4 hours later")):
        delay_line(label, ok, name)
        if name in ("lvl", "d0", "d15"):
            delay_line("   early half", [r for r in ok if r["t"] <= half], name)
            delay_line("   late half", [r for r in ok if r["t"] > half], name)
    for name, label, key in (("d60", "1h", "ret_1h"), ("d4h", "4h", "ret_4h")):
        up = [r for r in ok if name in r and r["d0"][key] > 0 and r[name]["gap"] > 0]
        dn = [r for r in ok if name in r and not (r["d0"][key] > 0 and r[name]["gap"] > 0)]
        delay_line(f"{label} later, only if up and above the old high", up, name)
        delay_line(f"{label} later, the others (faded)", dn, name)
    vol = [(r, r["d0"].get("vol_1h_after_vs_before")) for r in ok if "d60" in r]
    delay_line("1h later, volume kept up (>= 100% of before)", [r for r, x in vol if x is not None and x >= 1], "d60")
    delay_line("1h later, volume dried up (< 50% of before)", [r for r, x in vol if x is not None and x < 0.5], "d60")

    # ---- F3. the state after 24h and the rest of the month
    P("")
    P("   After 24 hours -> what the REST of the 30 days did (from the price at 24h):")
    r24, r30, ab = s.col("ret_24h"), s.col("ret_30d"), s.col("above_old_24h")
    fwd = ((1 + r30 / 100) / (1 + r24 / 100) - 1) * 100
    v = np.isfinite(fwd)
    out["after_24h"] = {}
    for label, m in (("above the old high and up", (ab > 0) & (r24 > 0)), ("above the old high, up 10%+", (ab > 0) & (r24 >= 10)),
                     ("back below the old high", ab == 0), ("down 5%+ from the entry", r24 <= -5)):
        m = m & v
        if m.sum() >= 20:
            out["after_24h"][label] = {"n": int(m.sum()), "median": float(np.median(fwd[m])), "up20": float((fwd[m] >= 20).mean()),
                                       "down20": float((fwd[m] <= -20).mean())}
            P(f"     {label:30} n={m.sum():5,}  median {np.median(fwd[m]):+6.1f}%  ended +20% or more {share(fwd[m] >= 20)}  "
              f"ended -20% or worse {share(fwd[m] <= -20)}")

    # ---- G. by year / market
    P("")
    P("G. BY YEAR AND BY BITCOIN'S TREND (+10 / -4 within 48h; best run in 30 days)")
    res, code = s.race(10, 4)
    res53, code53 = s.race(5, 3)
    m30 = s.col("mfe_30d")
    years = np.array([datetime.fromtimestamp(t / 1000, tz=timezone.utc).year for t in s.t])
    btc_up = np.array([r["f"].get("btc_above_200d") for r in s.recs], object)
    kinds = np.array([r["kind"] for r in s.recs])
    out["by_group"] = {}

    def line(label: str, m: np.ndarray) -> None:
        if m.sum() < 10:
            return
        v_ = m & np.isfinite(m30)
        med = float(np.median(m30[v_])) if v_.any() else float("nan")
        out["by_group"][label] = {"n": int(m.sum()), "avg_10_4": float(np.nanmean(res[m])), "avg_5_3": float(np.nanmean(res53[m])),
                                  "median_run_30d": med}
        P(f"  {label:34} n={m.sum():5,}  +10/-4: {np.nanmean(res[m]):+5.2f} (hit {100 * (code[m] == 1).mean():3.0f}%)  "
          f"+5/-3: {np.nanmean(res53[m]):+5.2f}  median best run in 30d {med:+6.1f}%  "
          f"reached +30% {share(m30[v_] >= 30)}  +100% {share(m30[v_] >= 100)}")
    for y in sorted(set(years)):
        line(str(y), years == y)
    line("Bitcoin above its 200-day average", btc_up == True)   # noqa: E712
    line("Bitcoin below its 200-day average", btc_up == False)  # noqa: E712
    line("All-history high", kinds == "AH")
    line("52-week high", kinds == "52W")
    liq = np.array([r["f"]["liq_usd_30d"] for r in s.recs])
    for label, lo_, hi_ in (("daily volume under $5M", 0, 5e6), ("daily volume $5M-$50M", 5e6, 5e7), ("daily volume over $50M", 5e7, 1e18)):
        line(label, (liq >= lo_) & (liq < hi_))

    # ---- H. what the big runners had in common
    P("")
    P("H. WHAT WAS DIFFERENT AT THE ENTRY? (measures known at the alert; listed only if the link has the same sign in BOTH halves)")
    feats: dict[str, np.ndarray] = {}
    for k in sorted({k for r in s.recs for k in r["f"]}):
        feats[k] = np.array([float(r["f"][k]) if r["f"].get(k) is not None else np.nan for r in s.recs])
    feats["gap_pct"] = s.col("gap")
    feats["is_all_history_high"] = (kinds == "AH").astype(float)
    feats["log10_daily_volume_usd"] = np.log10(np.maximum(feats.pop("liq_usd_30d"), 1))
    pairs_ = np.array([r["pair"] for r in s.recs])
    order_t = s.t                                                        # ok is sorted by time
    feats["breaks_all_coins_prev_7d"] = np.array([np.searchsorted(order_t, t) - np.searchsorted(order_t, t - 7 * DAY)
                                                  for t in order_t], float)
    prev30 = np.zeros(len(order_t))
    for p in set(pairs_):
        idx = np.where(pairs_ == p)[0]
        tt = order_t[idx]
        prev30[idx] = [np.searchsorted(tt, t) - np.searchsorted(tt, t - 30 * DAY) for t in tt]
    feats["same_coin_breaks_prev_30d"] = prev30
    for k in ("funding_pct", "funding_3d_avg_pct", "oi_chg_1h_pct", "oi_chg_4h_pct", "oi_chg_24h_pct", "long_short_accounts"):
        feats[k] = np.array([float(fut.get(r["key"], {}).get(k)) if fut.get(r["key"], {}).get(k) is not None else np.nan
                             for r in s.recs])
    oi_usd = np.array([float(fut.get(r["key"], {}).get("oi_usd") or np.nan) for r in s.recs])
    feats["oi_vs_daily_volume"] = oi_usd / liq
    res104, code104 = res, code
    r2010, c2010 = s.race(20, 10, "30")
    targets = {"+10 before -4 within 48h": (code104 == 1, np.isfinite(res104)),
               "best run in 30d >= +30%": (m30 >= 30, np.isfinite(m30)),
               "best run in 30d >= +100%": (m30 >= 100, np.isfinite(m30)),
               "+20 before -10 within 30d": (c2010 == 1, np.isfinite(r2010)),
               "still +20% or more after 30 days": (s.col("ret_30d") >= 20, np.isfinite(s.col("ret_30d")))}
    out["factors"] = {}

    def rank(a: np.ndarray) -> np.ndarray:
        return np.argsort(np.argsort(a)).astype(float)

    def rho(a: np.ndarray, b_: np.ndarray) -> float:
        if len(a) < 80 or np.std(a) == 0 or np.std(b_) == 0:
            return float("nan")
        return float(np.corrcoef(rank(a), rank(b_))[0, 1])

    def by_bin(xs: np.ndarray, hs: np.ndarray) -> tuple[list[str], list[float]]:
        uniq = np.unique(xs)
        if len(uniq) <= 5:
            return [f"{u:g}" for u in uniq], [float(hs[xs == u].mean() * 100) if (xs == u).sum() >= 10 else float("nan") for u in uniq]
        cut = np.unique(np.percentile(xs, [20, 40, 60, 80]))
        bins = np.digitize(xs, cut, right=True)
        labels = [f"<= {cut[0]:.3g}"] + [f"{a:.3g}..{b_:.3g}" for a, b_ in zip(cut[:-1], cut[1:])] + [f"> {cut[-1]:.3g}"]
        return labels, [float(hs[bins == b_].mean() * 100) if (bins == b_).sum() >= 10 else float("nan") for b_ in range(len(cut) + 1)]

    for tname, (hit, valid) in targets.items():
        rows = []
        for k, x in feats.items():
            v_ = valid & np.isfinite(x)
            if v_.sum() < 160:
                continue
            early = s.t <= np.median(s.t[v_])                          # halves of the events that HAVE this measure
            r1, r2 = rho(x[v_ & early], hit[v_ & early].astype(float)), rho(x[v_ & ~early], hit[v_ & ~early].astype(float))
            if not (np.isfinite(r1) and np.isfinite(r2)) or r1 * r2 <= 0 or min(abs(r1), abs(r2)) < 0.06:
                continue
            labels, rates = by_bin(x[v_], hit[v_])
            rows.append({"feature": k, "n": int(v_.sum()), "rho_early": r1, "rho_late": r2, "bins": labels, "rate_pct": rates,
                         "base": float(hit[v_].mean() * 100)})
        rows.sort(key=lambda r: -min(abs(r["rho_early"]), abs(r["rho_late"])))
        out["factors"][tname] = rows
        P(f"  {tname}  (overall {hit[valid].mean() * 100:.0f}% of {valid.sum():,})")
        for r in rows[:10]:
            P(f"    {r['feature']:26} n={r['n']:5,} link {r['rho_early']:+.2f} / {r['rho_late']:+.2f}   " +
              "   ".join(f"{lab}: {x:.0f}%" for lab, x in zip(r["bins"], r["rate_pct"])))
        if not rows:
            P("    nothing consistent in both halves")

    # ---- I. futures coverage
    P("")
    have = [r for r in s.recs if "funding_pct" in fut.get(r["key"], {})]
    have_oi = [r for r in s.recs if "oi_chg_24h_pct" in fut.get(r["key"], {})]
    P(f"I. FUTURES DATA: funding known for {len(have):,} breaks, open interest for {len(have_oi):,} "
      f"(Binance perpetual with the same name; open-interest files exist from December 2021)")
    if have:
        fr = np.array([fut[r["key"]]["funding_pct"] for r in have])
        P(f"  funding at the break, % per interval: median {np.median(fr):+.4f}, 75% {np.percentile(fr, 75):+.4f}, "
          f"90% {np.percentile(fr, 90):+.4f}  (the cost tables assume {FUNDING_PER_8H}% per 8h)")
        out["funding_at_break"] = np.percentile(fr, [25, 50, 75, 90]).tolist()
    if have_oi:
        oc = np.array([fut[r["key"]]["oi_chg_24h_pct"] for r in have_oi])
        P(f"  open interest change in the 24h before the break: median {np.median(oc):+.1f}%, "
          f"[25%..75%] {np.percentile(oc, 25):+.1f}..{np.percentile(oc, 75):+.1f}%")
        out["oi_chg_24h_before"] = np.percentile(oc, [25, 50, 75]).tolist()
    text = "\n".join(L)
    print(text)
    base = Path(cfg.data_dir)
    (base / "highs_breakout_report.txt").write_text(text, encoding="utf-8")
    (base / "highs_breakout_report.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\nSaved: {base / 'highs_breakout_report.txt'} and .json")
    return out
