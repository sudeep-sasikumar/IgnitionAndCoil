"""Pattern miner: turns recorded rows (research.history / research.recorder) into findings.

    .venv\\Scripts\\python.exe -m research.mine --history y2025,y2026      (two years, from history)
    .venv\\Scripts\\python.exe -m research.mine --live                    (recorded live days)

Every finding is measured in two separate periods (history: the two years; live: first vs second
half of the recorded days) and only reported as consistent if it holds in BOTH - the guard
against patterns that are just noise. Sections:
 1. overview      base rates: how often a bar leads to +tp% before -sl%, big moves, signals
 2. near_misses   breakouts blocked by exactly ONE active condition: would they have won?
 3. edges         which features separate winning from losing breakouts (deciles, both periods)
 4. missed_moves  big moves (+big_move_pct within 4h): did we signal? if not, what blocked us;
                  what the start of big moves looks like vs an ordinary bar
 5. rules         simple 1- and 2-feature rules found in period A, tested on period B
                  (seeds for a new system; target = "+tp% first" and "big move")
 6. live_only     order book / premium / OI features (live recordings only)
Output: <data_dir>/research/reports/<name>.json (shown on the Research tab) + a text summary.
"""
from __future__ import annotations

import argparse
import gzip
import itertools
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_config  # noqa: E402
from research.labels import label_arrays  # noqa: E402
from research.schema import CONDS, FEATURES, IGN_KEYS, LABELS, LIVE, MARKET  # noqa: E402

FEATS = [f for f in FEATURES if f not in ("hour_utc", "weekday")] + ["score", "breadth", "btc_ret_1h", "btc_ret_45m"]
MIN_SUPPORT = 150


def _f(x, nd=3):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), nd)


# ---- loading ------------------------------------------------------------------------------------

def load_history(cfg, tags: list[str]) -> pd.DataFrame:
    parts = []
    for t in tags:
        df = pd.read_csv(Path(cfg.data_dir) / "research" / "history" / f"{t}.csv.gz", low_memory=False)
        df["period"] = t
        parts.append(df)
    return pd.concat(parts, ignore_index=True)


def load_live(cfg, last_days: int | None = None) -> pd.DataFrame:
    r = cfg.research
    files = sorted((Path(cfg.data_dir) / "research" / "snapshots").glob("*.csv.gz"))
    files = files[-int(last_days or r.get("mine_last_days", 30)):]
    if not files:
        return pd.DataFrame()
    parts = []
    for p in files:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            parts.append(pd.read_csv(fh, low_memory=False))
    df = pd.concat(parts, ignore_index=True).drop_duplicates(["ts", "symbol"]).sort_values(["symbol", "ts"])
    for k in LABELS:
        df[k] = np.nan
    for sym, g in df.groupby("symbol"):
        lab = label_arrays(g["ts"].to_numpy(np.int64), g["h"].to_numpy(float), g["l"].to_numpy(float),
                           g["c"].to_numpy(float), float(r.tp_pct), float(r.sl_pct))
        for k in LABELS:
            df.loc[g.index, k] = lab[k]
    df["sample"] = np.where(df["c_break"] == 1, "break", "other")
    mid = df["ts"].min() + (df["ts"].max() - df["ts"].min()) / 2
    df["period"] = np.where(df["ts"] < mid, "first half", "second half")
    return df.reset_index(drop=True)


# ---- analyses -------------------------------------------------------------------------------------

def overview(df: pd.DataFrame, big: float) -> list[dict]:
    out = []
    for p, g in df.groupby("period", sort=True):
        tp = g["tp3_first"].dropna()
        out.append({"period": p, "rows": int(len(g)), "coins": int(g["symbol"].nunique()),
                    "days": _f((g["ts"].max() - g["ts"].min()) / 86_400_000, 1),
                    "breakouts": int((g["c_break"] == 1).sum()), "signals": int(g["signal"].sum()),
                    "sent": int(g["sent"].sum()), "tp_first_rate": _f(tp.mean()), "mean_fwd_4h": _f(g["fwd_4h"].mean()),
                    "big_move_rate": _f((g["mfe_4h"] >= big).mean())})
    return out


def _outcome(g: pd.DataFrame) -> dict:
    d = {"n": int(len(g)), "tp_first": _f(g["tp3_first"].mean()), "fwd_4h": _f(g["fwd_4h"].mean()),
         "mfe_4h": _f(g["mfe_4h"].median())}
    if "trade_r" in g and g["trade_r"].notna().any():
        d.update(trade_n=int(g["trade_r"].notna().sum()), trade_r=_f(g["trade_r"].mean()),
                 trade_sum_r=_f(g["trade_r"].sum(), 1))
    return d


def active_conds(cfg) -> list[str]:
    dis = set(cfg.signals.disabled_conditions or [])
    return [c for c, k in zip(CONDS, IGN_KEYS) if k not in dis and k.split(".")[1] not in dis
            and k != "ignition.break" and not (k == "ignition.oi" and "oi" in dis)]


def near_misses(df: pd.DataFrame, cfg) -> dict:
    """Breakout bars where every active condition but ONE held (score ignored). A condition that
    has no data in a period (e.g. funding before WEEX's history starts) counts as switched off there,
    exactly as the backtest treats it."""
    act = [c for c in active_conds(cfg) if df[c].notna().any() and (df[c] != -1).any()]
    b = df[df["c_break"] == 1]
    res = {"conditions": [c[2:] for c in act], "fired": {}, "blockers": []}
    per = {}
    for p, g in b.groupby("period"):
        live = [c for c in act if (g[c] == -1).mean() < 0.95]     # has data in this period
        ok = g[live] == 1
        per[p] = (g, live, (~ok).sum(axis=1))
        res["fired"][p] = _outcome(g[g["signal"] == 1])
    for c in act:
        row = {"condition": c[2:]}
        for p, (g, live, fails) in per.items():
            row[p] = _outcome(g[(fails == 1) & (g[c] != 1)]) if c in live else {"n": 0, "note": "no data"}
        res["blockers"].append(row)
    return res


def _deciles(x: pd.Series, bins: int = 10) -> np.ndarray | None:
    q = np.unique(np.nanquantile(x.dropna(), np.linspace(0, 1, bins + 1)))
    return q if len(q) >= 4 else None


def _spearman(a, b) -> float:
    a, b = np.asarray(a, float), np.asarray(b, float)
    m = ~(np.isnan(a) | np.isnan(b))
    if m.sum() < 4:
        return math.nan
    ra, rb = pd.Series(a[m]).rank().to_numpy(), pd.Series(b[m]).rank().to_numpy()
    return float(np.corrcoef(ra, rb)[0, 1])


def edges(df: pd.DataFrame, target: str, feats: list[str], bins: int = 10, min_bin: int = 20) -> list[dict]:
    """Per feature: mean target by decile in each period; consistent = same monotonic direction."""
    out = []
    periods = sorted(df["period"].unique())
    d = df[df[target].notna()]
    for f in feats:
        if f not in d or d[f].notna().sum() < 10 * MIN_SUPPORT // 10:
            continue
        q = _deciles(d[f], bins)
        if q is None:
            continue
        idx = np.clip(np.searchsorted(q, d[f].to_numpy(), side="right") - 1, 0, len(q) - 2)
        idx = np.where(d[f].isna(), -1, idx)
        rows = {}
        rhos = []
        for p in periods:
            m = (d["period"] == p).to_numpy()
            means = [float(d[target][m & (idx == i)].mean()) if (m & (idx == i)).sum() >= min_bin else math.nan
                     for i in range(len(q) - 1)]
            rows[p] = [_f(x) for x in means]
            rhos.append(_spearman(range(len(means)), means))
        consistent = all(np.isfinite(rhos)) and (all(r >= 0.5 for r in rhos) or all(r <= -0.5 for r in rhos))
        out.append({"feature": f, "edges": [_f(x, 4) for x in q], "by_decile": rows,
                    "rho": [_f(r, 2) for r in rhos], "consistent": bool(consistent),
                    "direction": "higher is better" if np.nanmean(rhos) > 0 else "lower is better"})
    out.sort(key=lambda r: (not r["consistent"], -min(abs(x or 0) for x in r["rho"])))
    return out


def missed_moves(df: pd.DataFrame, cfg, big: float) -> dict:
    """Starts of big moves (first qualifying row per coin within 4h) and whether we caught them."""
    d = df.sort_values(["symbol", "ts"])
    is_big = d["mfe_4h"] >= big
    starts = []
    last = {}
    for i, (sym, ts, b) in enumerate(zip(d["symbol"], d["ts"], is_big)):
        if b and ts - last.get(sym, -10 ** 15) > 4 * 3_600_000:
            starts.append(d.index[i])
            last[sym] = ts
    s = d.loc[starts]
    sig = d[d["signal"] == 1][["symbol", "ts"]]
    caught = []
    for sym, ts in zip(s["symbol"], s["ts"]):
        x = sig[(sig["symbol"] == sym) & (sig["ts"] >= ts - 3_600_000) & (sig["ts"] <= ts + 3_600_000)]
        caught.append(len(x) > 0)
    s = s.assign(caught=caught)
    act = active_conds(cfg)
    blockers = {}
    for _, r in s[~s["caught"]].iterrows():
        if r.get("c_break") != 1:
            k = "no 12h-high breakout at that moment"
        else:
            f = [c[2:] for c in act if r.get(c) != 1]
            k = "blocked by " + (f[0] if len(f) == 1 else f"{len(f)} conditions") if f else "score / cooldown / gate"
        blockers[k] = blockers.get(k, 0) + 1
    prof = []
    for f in FEATS:
        if f not in d or d[f].notna().sum() < 100:
            continue
        smd = []
        for p in sorted(d["period"].unique()):
            a, allp = s[s["period"] == p][f].dropna(), d[d["period"] == p][f].dropna()
            sd = allp.std()
            smd.append((a.mean() - allp.mean()) / sd if len(a) >= 20 and sd > 0 else math.nan)
        cons = all(np.isfinite(smd)) and (all(x >= 0.2 for x in smd) or all(x <= -0.2 for x in smd))
        prof.append({"feature": f, "smd": [_f(x, 2) for x in smd], "consistent": bool(cons)})
    prof.sort(key=lambda r: (not r["consistent"], -min(abs(x or 0) for x in r["smd"])))
    by_p = {p: {"moves": int((s["period"] == p).sum()), "caught": int(s[s["period"] == p]["caught"].sum())}
            for p in sorted(d["period"].unique())}
    return {"big_move_pct": big, "by_period": by_p,
            "why_missed": dict(sorted(blockers.items(), key=lambda kv: -kv[1])), "profile": prof[:15]}


def rules(df: pd.DataFrame, target: str, feats: list[str], label: str, min_support: int = MIN_SUPPORT,
          signed: bool = False) -> dict:
    """1- and 2-feature threshold rules picked on period A, checked on period B. Rates (win proxy,
    big move) are compared as lift = rate / base; signed outcomes (trade R) as difference = mean - base.
    Returns rules that IMPROVE on the base and rules to AVOID, each with its result on both periods."""
    periods = sorted(df["period"].unique())
    if len(periods) < 2:
        return {"target": label, "note": "needs two periods"}
    A, B = periods[0], periods[-1]
    d = df[df[target].notna()]
    a, b = d[d["period"] == A], d[d["period"] == B]
    base_a, base_b = float(a[target].mean()), float(b[target].mean())
    cands = []
    for f in feats:
        if f not in d or a[f].notna().sum() < 3 * min_support:
            continue
        for qv in np.unique(np.nanquantile(a[f].dropna(), [0.1, 0.2, 0.3, 0.5, 0.7, 0.8, 0.9])):
            for op in (">=", "<="):
                cands.append((f, op, float(qv)))

    def mask(g, rule):
        m = np.ones(len(g), bool)
        for f, op, v in rule:
            x = g[f].to_numpy(float)
            m &= (x >= v) if op == ">=" else (x <= v)
        return m

    def effect(rate, base):
        return rate - base if signed else (rate / base if base else math.nan)

    def score(g, rule, base):
        m = mask(g, rule)
        n = int(m.sum())
        if n < min_support:
            return None
        sub = g[m]
        rate = float(sub[target].mean())
        return {"n": n, "rate": _f(rate), "effect": _f(effect(rate, base), 3), "fwd_4h": _f(sub["fwd_4h"].mean()),
                "tp_first": _f(sub["tp3_first"].mean()), "mae_4h": _f(sub["mae_4h"].median())}

    def search(better: bool):
        key = (lambda s_: -s_["effect"]) if better else (lambda s_: s_["effect"])
        singles = [([c], s_) for c in cands if (s_ := score(a, [c], base_a))]
        singles.sort(key=lambda x: key(x[1]))
        top = singles[:14]
        pairs = []
        for (r1, _), (r2, _) in itertools.combinations(top, 2):
            if r1[0][0] != r2[0][0] and (s_ := score(a, r1 + r2, base_a)):
                pairs.append((r1 + r2, s_))
        pairs.sort(key=lambda x: key(x[1]))
        neutral = 0.0 if signed else 1.0
        need = (0.1 if signed else 0.2) * (1 if better else -1)
        out = []
        for rule, sa in singles[:20] + pairs[:30]:
            sb = score(b, rule, base_b)
            holds = bool(sb) and ((sb["effect"] - neutral >= need) if better else (sb["effect"] - neutral <= need))
            out.append({"rule": " and ".join(f"{f} {op} {v:.4g}" for f, op, v in rule), A: sa, B: sb, "holds": holds})
        worst = (lambda r: -min(r[A]["effect"], (r[B] or {}).get("effect", -1e9))) if better else                 (lambda r: max(r[A]["effect"], (r[B] or {}).get("effect", 1e9)))
        out.sort(key=lambda r: (not r["holds"], worst(r)))
        return out[:20]

    return {"target": label, "measure": "difference (R)" if signed else "lift (x base)",
            "base": {A: _f(base_a), B: _f(base_b)}, "found_on": A, "tested_on": B,
            "rules": search(True), "avoid": search(False)}


def run(df: pd.DataFrame, cfg, name: str) -> dict:
    r = cfg.research
    big = float(r.big_move_pct)
    df = df.copy()
    df["big_move"] = (df["mfe_4h"] >= big).astype(float).where(df["mfe_4h"].notna())
    brk = df[df["c_break"] == 1]
    sig_rows = df[df["sent"] == 1]
    tgt = "trade_r" if "trade_r" in df and df["trade_r"].notna().any() else "tp3_first"
    live_feats = [f for f in LIVE if f in df and df[f].notna().sum() > 200]
    rep = {
        "name": name, "generated_ms": int(time.time() * 1000), "config": cfg.hash,
        "params": {"tp_pct": float(r.tp_pct), "sl_pct": float(r.sl_pct), "big_move_pct": big,
                   "breakout_target": tgt},
        "overview": overview(df, big),
        "near_misses": near_misses(df, cfg),
        "edges": edges(brk, tgt, FEATS + live_feats),
        "missed_moves": missed_moves(df, cfg, big),
        "rules_tp": rules(df, "tp3_first", FEATS + live_feats, f"+{r.tp_pct:g}% before -{r.sl_pct:g}% within 4h"),
        "rules_big": rules(df, "big_move", FEATS + live_feats, f"a +{big:g}% move within 4h"),
        "rules_breakout": rules(brk, tgt, FEATS + live_feats,
                                "Policy S trade R on breakouts" if tgt == "trade_r" else "+tp first on breakouts",
                                signed=tgt == "trade_r"),
        "signal_edges": edges(sig_rows, tgt, FEATS + live_feats, bins=5, min_bin=15),
        "rules_signals": rules(sig_rows, tgt, FEATS + live_feats,
                               "trade R of the signals your system SENT" if tgt == "trade_r" else "+tp first of sent signals",
                               min_support=40, signed=tgt == "trade_r"),
        "live_only": edges(brk, "tp3_first", live_feats) if live_feats else [],
    }
    return rep


def summary(rep: dict) -> str:
    L = [f"== {rep['name']} =="]
    for o in rep["overview"]:
        L.append(f"{o['period']}: {o['rows']:,} rows, {o['coins']} coins, {o['breakouts']:,} breakouts, {o['signals']} signals "
                 f"({o['sent']} sent) | +tp first {o['tp_first_rate']} | big-move rate {o['big_move_rate']}")
    nm = rep["near_misses"]
    L.append("Near misses (one active condition short):")
    for b in nm["blockers"]:
        L.append("  " + b["condition"].ljust(12) + " | ".join(
            f"{p}: n={v['n']} R={v.get('trade_r')} tp={v.get('tp_first')}" + (" (no data)" if v.get("note") else "")
            for p, v in b.items() if p != "condition"))
    L.append("  signals that fired: " + " | ".join(f"{p}: n={v['n']} R={v.get('trade_r')} tp={v.get('tp_first')}"
                                                 for p, v in nm["fired"].items()))
    L.append("Consistent breakout edges:")
    for e in [e for e in rep["edges"] if e["consistent"]][:10]:
        L.append(f"  {e['feature']:14s} {e['direction']:17s} rho={e['rho']}")
    mm = rep["missed_moves"]
    L.append(f"Big moves (+{mm['big_move_pct']}% in 4h): {mm['by_period']}  why missed: {mm['why_missed']}")
    for p in [p for p in mm["profile"] if p["consistent"]][:8]:
        L.append(f"  before big moves: {p['feature']:14s} SMD={p['smd']}")
    for e in [e for e in rep.get("signal_edges", []) if e["consistent"]][:8]:
        L.append(f"  sent signals: {e['feature']:14s} {e['direction']:17s} rho={e['rho']}")
    for key in ("rules_signals", "rules_breakout", "rules_tp", "rules_big"):
        rr = rep.get(key) or {}
        if "rules" not in rr:
            continue
        A, B = rr["found_on"], rr["tested_on"]
        L.append(f"Rules for {rr['target']} [{rr['measure']}] base {rr['base']} - found on {A}, tested on {B}:")
        for kind in ("rules", "avoid"):
            for x in [x for x in rr[kind] if x["holds"]][:5]:
                fa, fb = x[A], x[B] or {}
                L.append(f"  {'IMPROVE' if kind == 'rules' else 'AVOID  '} {x['rule']}: {A} n={fa['n']} {fa['rate']} "
                         f"({fa['effect']}) -> {B} n={fb.get('n')} {fb.get('rate')} ({fb.get('effect')}) | "
                         f"fwd4h {fa['fwd_4h']}/{fb.get('fwd_4h')} tp-first {fa['tp_first']}/{fb.get('tp_first')}")
    return "\n".join(L)


def save(cfg, rep: dict, name: str) -> Path:
    d = Path(cfg.data_dir) / "research" / "reports"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    p.write_text(json.dumps(rep, indent=1, default=lambda x: None), encoding="utf-8")
    return p


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Pattern miner")
    ap.add_argument("--history", default=None, help="comma-separated history tags, e.g. y2025,y2026")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--days", type=int, default=None, help="live: most recent N days (default research.mine_last_days)")
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    cfg = load_config(a.config)
    if a.history:
        rep = run(load_history(cfg, a.history.split(",")), cfg, "history")
    else:
        df = load_live(cfg, a.days)
        if df.empty:
            print("no live recordings yet")
            return 1
        rep = run(df, cfg, "live")
    p = save(cfg, rep, rep["name"])
    print(summary(rep))
    print(f"Saved: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
