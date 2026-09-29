"""Backtester: runs the live code on history; end-to-end no-lookahead; report renders."""
import copy

import numpy as np
import pytest

from backtest.data import FIELDS, merge_arrays
from backtest.engine import Backtester, BTInputs, window
from backtest.report import build
from core.clock import TF_MS
from core.config import Config
from data.bars import BarArrays
from exchange.models import Bar
from plan.liquidation import parse_brackets

M5 = TF_MS["5m"]
DAY = 86_400_000
T_START = 1_780_000_000_000 - 1_780_000_000_000 % (4 * 3_600_000)   # 4h-aligned
BRK = parse_brackets([{"bracket": 1, "notionalFloor": "0", "notionalCap": "2000000", "initialLeverage": 300,
                       "maintMarginRatio": "0.002"}])


def synth_5m(seed: int, n: int, start: int, drift: float = 0.0) -> list[Bar]:
    rng = np.random.default_rng(seed)
    price = 100.0
    out = []
    for i in range(n):
        r = rng.normal(drift, 0.004)
        if rng.random() < 0.004:        # occasional impulsive breakout
            r = abs(r) + 0.02
        o, c = price, price * (1 + r)
        h, l = max(o, c) * (1 + abs(rng.normal(0, 0.001))), min(o, c) * (1 - abs(rng.normal(0, 0.001)))
        v = float(rng.uniform(5e3, 2e4)) * (6 if abs(r) > 0.015 else 1)
        buy = v * float(rng.uniform(0.3, 0.8))
        out.append(Bar(start + i * M5, o, h, l, c, v, v * c, buy, buy * c, 10, start + (i + 1) * M5))
        price = c
    return out


def aggregate(b5: list[Bar], tf: str) -> list[Bar]:
    k = TF_MS[tf] // M5
    out = []
    for i in range(0, len(b5) - k + 1, k):
        g = b5[i:i + k]
        if g[0].t % TF_MS[tf]:
            continue
        out.append(Bar(g[0].t, g[0].o, max(b.h for b in g), min(b.l for b in g), g[-1].c, sum(b.v for b in g),
                       sum(b.qv for b in g), sum(b.tbv for b in g), sum(b.tbqv for b in g), 10, g[-1].tc))
    return out


def make_inputs(n_days: float = 4, cut: int | None = None) -> BTInputs:
    n = int(n_days * 288) + 12 * 288            # 12 days of warm-up history
    start = T_START - 12 * DAY
    bars = {}
    for i, sym in enumerate(["BTCUSDT", "AAAUSDT", "BBBUSDT", "CCCUSDT"]):
        b5 = synth_5m(i + 1, n, start, drift=0.0003 if i == 1 else 0.0)
        if cut is not None:
            b5 = [b for b in b5 if b.tc <= cut]
        bars[sym] = {"5m": BarArrays.from_bars(b5)}
        for tf in ("15m", "1h", "4h"):
            bars[sym][tf] = BarArrays.from_bars(aggregate(b5, tf))
    funding = {s: [(T_START + k * 8 * 3_600_000, 0.0001, 100.0) for k in range(-3, 13)
                   if cut is None or T_START + k * 8 * 3_600_000 <= cut] for s in bars}
    return BTInputs(bars=bars, funding=funding, precision={s: 4 for s in bars}, brackets={s: BRK for s in bars})


@pytest.fixture
def loose(cfg):
    """Thresholds opened up so synthetic data produces plenty of signals."""
    d = copy.deepcopy(cfg.to_dict())
    d["universe"].update(min_quote_volume_24h=1, min_atr_1h_pct=0.0, max_deep_wick_bars=10_000)
    d["signals"]["disabled_conditions"] = ["price", "volume", "trend", "momentum", "flow", "funding", "headroom",
                                           "candle", "squeeze"]
    d["score"]["min_entry"] = 30
    return Config(d, cfg.path)


def test_backtest_produces_signals_and_trades(loose):
    inp = make_inputs(3)
    res = Backtester(loose, inp, {"oi"}).run(T_START, T_START + 3 * DAY)
    assert res["bars"] == 3 * 288
    assert res["signals"], "expected signals on synthetic data"
    assert "oi" in res["disabled"]
    closed = [t for t in res["trades"] if t["status"] == "CLOSED"]
    assert closed
    for t in closed:
        assert t["entry_ms"] < t["exit_ms"] <= t["entry_ms"] + 8 * 3_600_000 + M5
        assert t["exit_reason"] in ("STOP", "TP2", "TIME_STOP", "EMA_EXIT", "MAX_HOLD", "LIQUIDATION")
        assert t["net"] == pytest.approx(t["r"] * t["risk_usd"])
    ids = [s["signal_id"] for s in res["signals"]]
    assert ids == sorted(ids) and ids[0] == "B-0001"


def test_backtest_no_lookahead_end_to_end(loose):
    """Signals and closed trades up to T must be identical whether or not data after T exists."""
    end = T_START + 3 * DAY
    cut = T_START + 2 * DAY
    full = Backtester(loose, make_inputs(3), {"oi"}).run(T_START, end)
    part = Backtester(loose, make_inputs(3, cut=cut), {"oi"}).run(T_START, cut)
    sig_full = [s for s in full["signals"] if s["bar_close_ms"] <= cut]
    assert sig_full, "need signals before the cut for a meaningful test"
    assert [(s["symbol"], s["setup"], s["bar_close_ms"], s["score"], s["plan"]["ref_entry"]) for s in sig_full] == \
           [(s["symbol"], s["setup"], s["bar_close_ms"], s["score"], s["plan"]["ref_entry"]) for s in part["signals"]]
    done_full = {(t["signal_id"], t["policy"]): (t["exit_ms"], t["exit_reason"], round(t["net"], 9))
                 for t in full["trades"] if t["status"] == "CLOSED" and t["exit_ms"] <= cut}
    done_part = {(t["signal_id"], t["policy"]): (t["exit_ms"], t["exit_reason"], round(t["net"], 9))
                 for t in part["trades"] if t["status"] == "CLOSED"}
    assert done_full == done_part


def test_funding_uses_last_settled_rate(loose):
    bt = Backtester(loose, make_inputs(1), {"oi"})
    f8, nxt = bt.funding_at("AAAUSDT", T_START + 3_600_000)
    assert f8 == pytest.approx(0.0001) and nxt == T_START + 8 * 3_600_000
    f8b, _ = bt.funding_at("AAAUSDT", T_START - 30 * DAY)
    assert f8b is None


def test_report_renders(loose):
    res = Backtester(loose, make_inputs(2), {"oi"}).run(T_START, T_START + 2 * DAY)
    html = build(res, {"symbols": ["BTCUSDT", "AAAUSDT"], "config_hash": "abc", "generated_ms": T_START,
                       "notes": ["<b>Open-interest conditions DISABLED.</b>"], "runtime_s": 1.0}, loose)
    assert html.startswith("<!doctype html>") and "Data &amp; limitations" in html
    assert "Open-interest conditions DISABLED" in html and "<svg" in html and "Policy L" in html
    assert "<script" not in html


def test_merge_and_window_helpers():
    a = BarArrays.from_bars(synth_5m(1, 10, 0))
    b = BarArrays.from_bars(synth_5m(2, 10, 5 * M5))          # overlaps bars 5..9
    m = merge_arrays(a, b)
    assert len(m) == 15 and list(m.t) == sorted(set(m.t)) and m.c[5] == b.c[0]   # later download wins
    w = window(m, 3 * M5, 7 * M5)
    assert list(w.t) == [3 * M5, 4 * M5, 5 * M5, 6 * M5]
    assert set(FIELDS) >= {"t", "tc"}


def test_funnel_counts(loose):
    res = Backtester(loose, make_inputs(1), {"oi"}).run(T_START, T_START + DAY)
    f = res["funnel"]
    ign = f["IGNITION"]
    assert ign["n"] > 0 and ign["conds"]
    assert all(0 <= c["pass_rate"] <= 1 for c in ign["conds"])
    # disabled groups count as passing
    oi = next(c for c in ign["conds"] if c["name"].startswith("oi_chg_1h"))
    assert oi["pass_rate"] == 1.0 and oi["sole_blocker"] == 0
    html = build(res, {"symbols": ["X"], "config_hash": "h", "generated_ms": T_START, "notes": [], "runtime_s": 1}, loose)
    assert "Condition funnel" in html and "Sole blocker" in html


def test_sweep_fast_path_matches_exact_backtest(loose):
    """Filtering loosest-setting candidates must reproduce a real run at tighter settings."""
    from backtest.__main__ import apply_overrides
    from backtest.sweep import LOOSEST, harvest_candidates, select

    def cfg_with(pairs):
        d = copy.deepcopy(loose.to_dict())
        d["coil"]["min_rvol_15m"] = 1e9            # Coil off: the fast path is exact for Ignition
        d["signals"]["disabled_conditions"] = [g for g in d["signals"]["disabled_conditions"]
                                               if g not in ("volume", "momentum")]
        apply_overrides(d, [f"{k}={v}" for k, v in pairs.items() if k != "coil.min_rvol_15m"])
        return Config(d, loose.path)

    inp = make_inputs(3)
    harvest = Backtester(cfg_with(LOOSEST), inp, {"oi"}).run(T_START, T_START + 3 * DAY)
    cands = harvest_candidates(harvest)
    combo = {"ignition.min_rvol_5m": 2.0, "ignition.min_rvol_15m": 1.2, "ignition.min_ret_1h": 0.5,
             "ignition.max_ret_1h": 8.0, "coil.min_rvol_15m": 1e9, "score.min_entry": 35}
    fast = [(c["symbol"], c["T"]) for c in select(cands, combo, int(loose.signals.symbol_cooldown_min) * 60_000)]
    exact = Backtester(cfg_with(combo), make_inputs(3), {"oi"}).run(T_START, T_START + 3 * DAY)
    assert fast, "need signals for a meaningful comparison"
    assert fast == [(s["symbol"], s["bar_close_ms"]) for s in exact["signals"]]


def test_aggregate_matches_bar_by_bar_definition():
    from backtest.data import aggregate as agg_np
    b5 = synth_5m(3, 300, T_START)
    ref = BarArrays.from_bars(aggregate(b5, "15m"))          # the test file's own loop version
    got = agg_np(BarArrays.from_bars(b5), "15m")
    for f in FIELDS:
        assert np.allclose(getattr(got, f), getattr(ref, f), rtol=1e-12), f


def test_study_resimulate_across_leverage(loose):
    from backtest.__main__ import apply_overrides
    from backtest.study import pick_combo, resimulate
    from backtest.sweep import LOOSEST
    d = copy.deepcopy(loose.to_dict())
    apply_overrides(d, [f"{k}={v}" for k, v in LOOSEST.items()])
    c = Config(d, loose.path)
    inp = make_inputs(3)
    bt = Backtester(c, inp, {"oi"})
    res = bt.run(T_START, T_START + 3 * DAY)
    assert res["signals"]
    sig = res["signals"][0]
    by_lev = {lev: resimulate(bt, sig, lev, T_START + 3 * DAY, inp) for lev in (50, 20, 10)}
    stops = {lev: by_lev[lev]["L"]["stop"] for lev in by_lev}
    assert stops[50] > stops[20] > stops[10]                 # lower leverage: liquidation (and L stop) further away
    for lev in (20, 10):
        assert ("S" in by_lev[lev]) >= ("S" in by_lev[50])  # S only becomes available, never lost
    risk = {lev: by_lev[lev]["L"]["risk_usd"] for lev in by_lev}
    assert risk[10] > risk[20] > risk[50]                    # same $1,000 position: wider stop = more $ at risk
    cands = [{"id": s["signal_id"], "symbol": s["symbol"], "setup": s["setup"], "T": s["bar_close_ms"],
              "score": s["score"], "rvol_5m": s["features"]["rvol_5m"], "rvol_15m": s["features"]["rvol_15m"],
              "ret_1h": s["features"]["ret_1h"], "trades": {"50x-L": t}} for s, t in
             ((s, resimulate(bt, s, 50, T_START + 3 * DAY, inp).get("L")) for s in res["signals"]) if t]
    combo, ins = pick_combo(cands, "50x-L", T_START + 3 * DAY, 0, 1)
    assert combo is None or set(combo) == set(__import__("backtest.sweep", fromlist=["GRID"]).GRID)
