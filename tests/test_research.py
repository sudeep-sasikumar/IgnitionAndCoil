"""Research layer: outcome labels (no lookahead into features), row layout, recorder files, miner."""
import gzip
import math

import numpy as np
import pandas as pd
import pytest

from research.labels import M5, label_arrays
from research.schema import COLUMNS, build_row, fmt
from tests.helpers import breakout_bars, features


def _bars(closes, highs=None, lows=None, t0=0):
    n = len(closes)
    t = np.arange(n, dtype=np.int64) * M5 + t0
    c = np.array(closes, float)
    h = np.array(highs if highs is not None else closes, float)
    l = np.array(lows if lows is not None else closes, float)
    return t, h, l, c


def test_labels_forward_returns_and_excursions():
    closes = [100.0] * 30 + [101.0] * 400                  # the step up happens at bar 30
    t, h, l, c = _bars(closes)
    lab = label_arrays(t, h, l, c, 3.0, 2.0)
    assert lab["fwd_1h"][0] == pytest.approx(0.0)           # bar 12 is still 100
    assert lab["fwd_1h"][20] == pytest.approx(1.0)          # bar 32 is 101
    assert lab["mfe_4h"][0] == pytest.approx(1.0) and lab["mae_4h"][0] == pytest.approx(0.0)
    assert math.isnan(lab["fwd_24h"][-1])                     # no future: no label


def test_tp_first_win_loss_and_same_bar():
    base = [100.0] * 60
    hi, lo = list(base), list(base)
    hi[3] = 103.5                                           # +3% on bar 3 ...
    lo[5] = 97.0                                            # ... before -2% on bar 5 -> win for bar 0
    t, h, l, c = _bars(base, hi, lo)
    assert label_arrays(t, h, l, c, 3.0, 2.0)["tp3_first"][0] == 1.0
    hi2, lo2 = list(base), list(base)
    hi2[4], lo2[4] = 104.0, 97.5                            # both in the same bar -> counted as a loss
    t, h, l, c = _bars(base, hi2, lo2)
    assert label_arrays(t, h, l, c, 3.0, 2.0)["tp3_first"][0] == 0.0
    t, h, l, c = _bars(base)
    assert math.isnan(label_arrays(t, h, l, c, 3.0, 2.0)["tp3_first"][0])   # neither within 4h


def test_labels_are_nan_across_gaps():
    t, h, l, c = _bars([100.0] * 100)
    t = np.r_[t[:50], t[50:] + 10 * M5]                      # 10 missing bars after bar 49
    lab = label_arrays(t, h, l, c, 3.0, 2.0)
    assert math.isnan(lab["fwd_1h"][45])                    # 1h later falls inside the gap
    assert math.isnan(lab["mfe_4h"][45])                    # the 4h window spans the gap
    assert not math.isnan(lab["fwd_1h"][10]) and not math.isnan(lab["fwd_4h"][45])   # 4h later exists again


def test_build_row_layout(cfg):
    b = breakout_bars()
    d = build_row(int(b.tc[-1]), features(), None, b, None, "abc", {"spread_pct": 0.02})
    assert set(COLUMNS) <= set(d) and d["symbol"] == "TSTUSDT" and d["spread_pct"] == 0.02
    assert d["break_pct"] > 0 and 0 <= d["close_pos"] <= 1 and d["signal"] == 0
    assert fmt(float("nan")) == "" and fmt(3.0) == "3" and fmt(0.123456789) == "0.123457"


def test_recorder_appends_readable_gzip(tmp_path):
    from research.recorder import Recorder

    class Eng:
        class cfg:
            data_dir = tmp_path
    rec = Recorder(Eng)
    row = {c: "" for c in COLUMNS}
    row.update(ts=1, symbol="A")
    rec.write(86_400_000, [row])
    rec.write(86_400_000, [{**row, "ts": 2}])                 # second append: another gzip member
    p = next((tmp_path / "research" / "snapshots").glob("*.csv.gz"))
    df = pd.read_csv(gzip.open(p, "rt"))
    assert list(df["ts"]) == [1, 2] and list(df.columns) == COLUMNS
