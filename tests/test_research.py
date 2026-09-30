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


def test_sync_accepts_only_complete_gzip_and_daily_names(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from sync_research import NAME, gzip_ok
    good = tmp_path / "a.csv.gz"
    with gzip.open(good, "wt") as fh:
        fh.write("x\n" * 1000)
    with gzip.open(good, "at") as fh:                       # appended member, like the recorder
        fh.write("y\n")
    bad = tmp_path / "b.csv.gz"
    bad.write_bytes(good.read_bytes()[:-8])                 # cut mid-member (download while writing)
    assert gzip_ok(good) and not gzip_ok(bad)
    assert NAME.fullmatch("2026-09-30.csv.gz") and not NAME.fullmatch("../x.csv.gz")


def test_book_stats_top_levels():
    from research.schema import book_stats
    bids = [["99.9", "10"], ["99.5", "20"], ["98.0", "1000"]]       # 98.0 is outside 0.5% of mid
    asks = [["100.1", "5"], ["100.4", "10"], ["102.0", "999"]]
    s = book_stats(bids, asks)
    assert s["book_spread_pct"] == pytest.approx(0.2)
    assert s["book_bid_05_usd"] == pytest.approx(99.9 * 10 + 99.5 * 20)
    assert s["book_ask_05_usd"] == pytest.approx(100.1 * 5 + 100.4 * 10)
    assert -1 < s["book_imbalance"] < 1 and s["book_reach_pct"] == pytest.approx(2.0)
    assert book_stats([], asks) == {}


def test_recorder_starts_a_new_file_when_the_layout_changes(tmp_path):
    from research.recorder import Recorder

    class Eng:
        class cfg:
            data_dir = tmp_path
    rec = Recorder(Eng)
    old = rec.dir / "2026-10-01.csv.gz"
    with gzip.open(old, "wt") as fh:                          # a file written by an older version
        fh.write("ts,symbol\n1,A\n")
    row = {c: "" for c in COLUMNS}
    from datetime import datetime, timezone
    row.update(ts=int(datetime(2026, 10, 1, 12, tzinfo=timezone.utc).timestamp() * 1000), symbol="B")
    rec.write(row["ts"], [row])
    assert (rec.dir / "2026-10-01.b.csv.gz").exists()
    rec.write(row["ts"], [row])                               # same layout: keeps appending to .b
    assert not (rec.dir / "2026-10-01.c.csv.gz").exists()
    assert len(pd.read_csv(gzip.open(rec.dir / "2026-10-01.b.csv.gz", "rt"))) == 2


def test_market_daystore_grows_and_survives_restart(tmp_path):
    from research.market import FIELDS, DayStore, book_top
    p = tmp_path / "market-2026-10-01.npz"
    s = DayStore(p)
    s.add(300_000, {"A": {"oi": 10.0, "funding_fc": 0.01}, "B": {"spread": 0.05}})
    s.add(600_000, {"A": {"oi": 11.0}, "C": {"bid05": 1000.0}})         # a coin joins mid-day
    s.add(600_000, {"B": {"oi": 5.0}})                                  # same bar again: filled in, not duplicated
    s.save()
    r = DayStore(p)                                                     # restart: continues the file
    assert r.ts == [300_000, 600_000] and r.symbols == ["A", "B", "C"]
    assert r.cols["oi"].dtype == np.float32 and r.cols["oi"].shape == (2, 3)
    assert r.cols["oi"][1, 0] == 11.0 and r.cols["oi"][1, 1] == 5.0 and np.isnan(r.cols["oi"][0, 2])
    assert set(FIELDS) == set(r.cols)
    sp, bid, ask = book_top([["99.9", "10"], ["98", "5"]], [["100.1", "2"]])
    assert sp == pytest.approx(0.2) and bid == pytest.approx(999.0) and ask == pytest.approx(200.2)


def test_sync_checks_npz_files(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from sync_research import MARKET, file_ok
    good = tmp_path / "market-2026-10-01.npz"
    np.savez_compressed(good, ts=np.arange(3), oi=np.ones((3, 2), np.float32))
    bad = tmp_path / "bad.npz"
    bad.write_bytes(good.read_bytes()[:-20])
    assert file_ok(good, good.name) and not file_ok(bad, "market-2026-10-02.npz")
    assert MARKET.fullmatch("market-2026-10-01.npz").group(1) == "2026-10-01"
