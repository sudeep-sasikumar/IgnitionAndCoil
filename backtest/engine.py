"""Backtester (spec §9): replays history through the SAME code as the live engine -
compute_features, compute_regime, SignalEngine (setups, score, plan) and the exit engine
(exits.context.replay) - with both stop policies.

No lookahead: at each simulated 5m close T, every input is cut with BarArrays.upto(T) and then
trimmed to the live ring-buffer length; funding uses the last settlement at or before T; OI
uses snapshots at or before T.
"""
from __future__ import annotations

import copy
import logging
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from core.clock import TF_MS
from core.config import Config
from data.bars import BarArrays
from data.oi import oi_change_pct
from data.universe import count_deep_wicks
from exits.context import replay
from exits.engine import make_params, new_state
from exits.short import make_params_short, price_range, replay_short
from features import indicators as ind
from features.compute import MarketInputs, compute_features
from paper.book import leg_kind
from plan.pnl import Leg, entry_fill, pnl_usd
from signals.engine import SignalEngine
from core.engine import default_policy_missing
from signals.regime import RISK_OFF, RISK_ON, compute_regime

log = logging.getLogger("backtest")

M5 = TF_MS["5m"]


@dataclass
class BTInputs:
    bars: dict[str, dict[str, BarArrays]]           # sym -> tf -> bars (history incl. warm-up)
    funding: dict[str, list[tuple[int, float, float]]]
    precision: dict[str, int | None] = field(default_factory=dict)
    brackets: dict[str, list] = field(default_factory=dict)
    first_bar: dict[str, int] = field(default_factory=dict)  # listing proxy
    oi: dict[str, tuple[list[int], list[float]]] = field(default_factory=dict)


class Funnel:
    """Why signals do / don't fire: per-condition pass rates, the sole blocker of near-misses,
    and the score distribution of evaluations that passed every hard condition."""

    def __init__(self):
        self.groups = {k: {"n": 0, "pass": Counter(), "sole": Counter(), "all_pass": 0, "scores": []}
                       for k in ("IGNITION", "COIL_WATCH", "COIL_ENTRY")}

    def _one(self, key: str, conds: list[dict], disabled: set[str], score: int | None) -> None:
        if not conds:
            return
        g = self.groups[key]
        g["n"] += 1
        failing = []
        for c in conds:
            if c["group"] in disabled or c["passed"]:
                g["pass"][c["name"]] += 1
            else:
                failing.append(c["name"])
        if not failing:
            g["all_pass"] += 1
            if score is not None:
                g["scores"].append(score)
        elif len(failing) == 1:
            g["sole"][failing[0]] += 1

    def add(self, row, disabled: set[str]) -> None:
        self._one("IGNITION", row.ignition_conds, disabled, row.score if row.setup == "IGNITION" else None)
        self._one("COIL_WATCH", row.watch_conds, disabled, None)
        self._one("COIL_ENTRY", row.coil_entry_conds, disabled, row.score if row.setup == "COIL" else None)

    def result(self, signals: list[dict]) -> dict:
        out = {}
        for k, g in self.groups.items():
            names = list(dict.fromkeys(list(g["pass"]) + list(g["sole"])))
            buckets = Counter("<50" if s < 50 else "50-59" if s < 60 else "60-69" if s < 70 else "70+"
                              for s in g["scores"])
            out[k] = {"n": g["n"], "all_pass": g["all_pass"], "score_buckets": dict(buckets),
                      "conds": [{"name": nm, "pass_rate": g["pass"][nm] / g["n"] if g["n"] else 0,
                                 "sole_blocker": g["sole"][nm]} for nm in names]}
        out["stop_s_na"] = dict(Counter(s["plan"]["stop_s"].get("note") or "?" for s in signals
                                        if s["plan"]["stop_s"]["price"] is None))
        return out


def window(a: BarArrays, t0: int, t1: int) -> BarArrays:
    """Bars with open time >= t0 and close time <= t1."""
    lo = int(np.searchsorted(a.t, t0))
    hi = int(np.searchsorted(a.tc, t1, side="right"))
    return BarArrays(*(getattr(a, f)[lo:max(lo, hi)] for f in ("t", "o", "h", "l", "c", "v", "qv", "tbv", "tbqv", "tc")))


def regime_block(sig, regime, cfg) -> str | None:
    """Regime gate (same as live for longs): longs are held back in RISK_OFF; shorts follow
    short.regime_rule - "risk_off" (only in RISK_OFF) or "not_risk_on" (RISK_OFF or NEUTRAL)."""
    state = regime.state if regime else "NEUTRAL"
    if sig.side == "SHORT":
        ok = state == RISK_OFF if cfg.short.regime_rule == "risk_off" else state != RISK_ON
        return None if ok else f"regime_{state.lower()}"
    return "risk_off" if "RISK_OFF" in sig.tags or state == RISK_OFF else None


def bt_config(cfg, disabled: set[str]) -> Config:
    d = copy.deepcopy(cfg.to_dict())
    d["signals"]["disabled_conditions"] = sorted(set(d["signals"].get("disabled_conditions") or []) | disabled)
    return Config(d, cfg.path)


class Backtester:
    def __init__(self, cfg, inp: BTInputs, disabled: set[str] | None = None):
        self.cfg = bt_config(cfg, disabled or set())
        self.inp = inp
        self.btc = cfg.universe.regime_reference
        self.keep = {tf: int(n) for tf, n in cfg.bars.keep.to_dict().items()}
        self.memo: dict = {}
        self._breadth_memo: dict = {}

    # ---- inputs as of T (no lookahead) ------------------------------------------------
    def arrays(self, sym: str, tf: str, T: int) -> BarArrays:
        a = self.inp.bars.get(sym, {}).get(tf)
        if a is None:
            return BarArrays.from_bars([])
        return a.upto_tail(T, self.keep[tf])

    def funding_at(self, sym: str, T: int) -> tuple[float | None, int | None]:
        rows = self.inp.funding.get(sym) or []
        ts = [r[0] for r in rows]
        i = int(np.searchsorted(ts, T, side="right")) - 1
        if i < 0:
            return None, None
        rate = rows[i][1]
        interval = (rows[i][0] - rows[i - 1][0]) if i > 0 else 8 * 3_600_000
        interval_min = max(interval // 60_000, 1)
        return rate * self.cfg.funding.normalise_to_min / interval_min, rows[i][0] + interval

    def market_inputs(self, sym: str, T: int) -> MarketInputs:
        f8, nxt = self.funding_at(sym, T)
        oi = self.inp.oi.get(sym)
        tol = self.cfg.open_interest.match_tolerance_s * 1000
        ch = (lambda m: oi_change_pct(oi[0], oi[1], T, m, tol)) if oi else (lambda m: None)
        return MarketInputs(oi_chg_15m=ch(15), oi_chg_1h=ch(60), oi_chg_4h=ch(240), funding_8h=f8, next_funding_ms=nxt)

    def universe_at(self, T: int) -> list[str]:
        """Spec §3 filters reconstructable from klines: 24h volume, ATR(1h)%, wick risk, listing age,
        stablecoins/majors, max_symbols. Spread and order-book depth have no history (not applied)."""
        u = self.cfg.universe
        out = []
        majors = set(u.majors)
        for sym in self.inp.bars:
            if sym in majors:
                continue
            b5 = self.arrays(sym, "5m", T)
            if len(b5) < 290 or int(b5.tc[-1]) != T:
                continue
            qv24 = float(b5.qv[-288:].sum())
            if qv24 < u.min_quote_volume_24h:
                continue
            first = self.inp.first_bar.get(sym)
            if first is not None and (T - first) / 86_400_000 < u.min_listing_age_days:
                continue
            h1 = self.arrays(sym, "1h", T).tail(100)
            if len(h1) <= u.atr_period:
                continue
            atr_pct = float(ind.atr(h1.h, h1.l, h1.c, u.atr_period)[-1] / h1.c[-1] * 100)
            if not atr_pct >= u.min_atr_1h_pct:
                continue
            if count_deep_wicks(b5, u.wick_lookback_bars_5m, u.deep_wick_pct) > u.max_deep_wick_bars:
                continue
            out.append((qv24, sym))
        out.sort(reverse=True)
        return [s for _, s in out[: u.max_symbols]]

    def breadth(self, syms: list[str], T: int) -> tuple[float, int]:
        n = above = 0
        p = self.cfg.regime.breadth_ema_1h
        for s in syms:
            b = self.arrays(s, "1h", T)
            if len(b) < p:
                continue
            key = (s, int(b.t[-1]), len(b))
            e = self._breadth_memo.get(key)
            if e is None:
                e = self._breadth_memo[key] = float(ind.ema(b.c, p)[-1])
            if np.isfinite(e):
                n += 1
                above += int(b.c[-1] > e)
        return (above / n * 100 if n else float("nan")), n

    # ---- main loop ------------------------------------------------------------------------
    def run(self, start_ms: int, end_ms: int, progress=None) -> dict:
        cfg = self.cfg
        se = SignalEngine(cfg)
        refresh = cfg.backtest.universe_refresh_min * 60_000
        universe: list[str] = []
        signals: list[dict] = []
        sent_times: list[int] = []
        n_bars = 0
        funnel = Funnel()
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
            bar_sigs = []
            for sym in universe:
                b5 = self.arrays(sym, "5m", T)
                if not len(b5) or int(b5.tc[-1]) != T:
                    continue
                b15, b1h, b4h = (self.arrays(sym, tf, T) for tf in ("15m", "1h", "4h"))
                if len(b15) < 5 or len(b1h) < 5:
                    continue
                f = compute_features(sym, b5, b15, b1h, btc5, self.market_inputs(sym, T), cfg, memo=self.memo)
                row, sigs, _w = se.evaluate(sym, T, f, b5, b15, b1h, b4h, regime, self.inp.brackets.get(sym),
                                            f.price, self.inp.precision.get(sym))
                funnel.add(row, se.disabled)
                bar_sigs += sigs
            # same Telegram-side gating as live (for the "sent vs suppressed" breakdown)
            for sig in sorted(bar_sigs, key=lambda s: s.score, reverse=True):
                rec = sig.to_record()
                recent = [t for t in sent_times if t > T - 3_600_000]
                reason = regime_block(sig, regime, cfg)
                if reason is None:
                    if any(t.startswith("BLACKOUT") for t in sig.tags) and not cfg.signals.blackout_alerts:
                        reason = "blackout"
                    elif default_policy_missing(rec, cfg):
                        reason = "default_policy_na"
                    elif len(recent) >= cfg.signals.max_alerts_per_hour:
                        reason = "hourly_cap"
                    else:
                        sent_times.append(T)
                rec["suppressed_reason"] = reason
                rec["signal_id"] = f"B-{len(signals) + 1:04d}"
                signals.append(rec)
            n_bars += 1
            d = T // 86_400_000
            if progress and d != day:
                day = d
                progress(T, len(signals), len(universe))
            T += M5
        trades = [t for s in signals for t in self.simulate(s, end_ms)]
        return {"signals": signals, "trades": trades, "bars": n_bars, "start_ms": start_ms, "end_ms": end_ms,
                "disabled": sorted(cfg.signals.disabled_conditions), "funnel": funnel.result(signals)}

    # ---- exits (same engine as live paper trades) -------------------------------------
    def simulate(self, sig: dict, end_ms: int) -> list[dict]:
        cfg = self.cfg
        plan = sig["plan"]
        sym = sig["symbol"]
        side = sig.get("side", "LONG")
        short = side == "SHORT"
        ref, qty = float(plan["ref_entry"]), float(plan["qty"])
        fill = entry_fill(ref, cfg, side=side)
        T = int(sig["bar_close_ms"])
        horizon = T + int(cfg.plan.max_hold_h * 3_600_000) + M5
        until = min(horizon, end_ms)
        b5 = window(self.inp.bars[sym]["5m"], T - M5, until)
        b15 = window(self.inp.bars[sym]["15m"], T - self.keep["15m"] * TF_MS["15m"], until)
        out = []
        for key in ("stop_l", "stop_s"):
            sp = plan[key]
            if sp.get("price") is None:
                continue
            stop = float(sp["price"])
            mk = make_params_short if short else make_params
            st = new_state(mk(fill, T, stop, float(plan["tp1"]), plan.get("tp2"), cfg, liq_price=plan.get("liq_price")))
            evs = (replay_short if short else replay)(st, b5, b15, cfg, until)
            lo, hi = price_range(st) if short else (st.lowest, st.highest)
            mfe = (1 - lo / fill) * 100 if short else (hi / fill - 1) * 100
            mae = (1 - hi / fill) * 100 if short else (lo / fill - 1) * 100
            legs = [{"ts": e.ts, "price": e.price, "fraction": e.fraction, "qty": qty * e.fraction,
                     "kind": leg_kind(e.kind), "reason": e.kind} for e in evs if e.kind != "STOP_MOVE"]
            risk = abs(pnl_usd(ref, qty, [Leg(stop, 1.0, "stop")], cfg, side=side))
            base = {"signal_id": sig["signal_id"], "policy": sp["policy"], "symbol": sym, "setup": sig["setup"],
                    "side": side,
                    "score": sig["score"], "regime": sig.get("regime", {}).get("state", "?"),
                    "session": sig["session"], "suppressed_reason": sig.get("suppressed_reason"),
                    "entry_ms": T, "entry_ref": ref, "stop": stop, "tp1": plan["tp1"], "tp2": plan.get("tp2"),
                    "risk_usd": risk, "legs": legs, "tags": sig.get("tags", [])}
            if not st.closed:
                out.append({**base, "status": "OPEN_AT_END", "net": None, "r": None, "exit_ms": None,
                            "exit_reason": None, "mfe_pct": mfe, "mae_pct": mae})
                continue
            fund = 0.0
            for ts, rate, mark in self.inp.funding.get(sym) or []:
                if T < ts <= st.exit_ms:
                    q_open = qty - sum(l["qty"] for l in legs if l["ts"] <= ts)
                    if q_open > 0:
                        # longs pay a positive rate, shorts receive it
                        fund += (1 if short else -1) * rate * q_open * (mark or ref)
            net = pnl_usd(ref, qty, [Leg(l["price"], l["fraction"], l["kind"]) for l in legs], cfg, funding_usd=fund,
                          side=side)
            out.append({**base, "status": "CLOSED", "net": net, "r": net / risk if risk else None,
                        "exit_ms": st.exit_ms, "exit_reason": st.exit_reason, "funding_usd": fund,
                        "tp1_ms": st.tp1_ms, "tp2_ms": st.tp2_ms, "mfe_pct": mfe, "mae_pct": mae})
        return out
