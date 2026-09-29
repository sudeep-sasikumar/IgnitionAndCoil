"""Stats metrics, bedtime session logic, DB backup rotation, funding helpers."""
import copy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.config import Config
from core.engine import Engine
from data.funding import funding_usd, qty_timeline
from stats.metrics import breakdown, london_hour, metrics, score_bucket

LON = ZoneInfo("Europe/London")


def tr(net, r, i):
    return {"net": net, "r": r, "entry_ms": i * 1000, "exit_ms": i * 1000 + 500}


def test_metrics_known_values():
    ts = [tr(10, 1.0, 1), tr(-5, -0.5, 2), tr(-5, -0.5, 3), tr(20, 2.0, 4), tr(-10, -1.0, 5)]
    m = metrics(ts)
    assert m["n"] == 5 and m["wins"] == 2 and m["win_rate"] == pytest.approx(0.4)
    assert m["net"] == 10 and m["expectancy_usd"] == pytest.approx(2.0)
    assert m["expectancy_r"] == pytest.approx(0.2)
    assert m["profit_factor"] == pytest.approx(30 / 20)
    assert m["max_consec_losses"] == 2
    # equity 10, 5, 0, 20, 10 -> peak 20 then 10: drawdown 10 (earlier 10 -> 0 also 10)
    assert m["max_drawdown_usd"] == pytest.approx(10)
    assert metrics([]) == {"n": 0}


def test_breakdown_and_buckets():
    ts = [dict(tr(5, 0.5, 1), setup="IGNITION"), dict(tr(-5, -1, 2), setup="COIL"), dict(tr(1, 0.1, 3), setup="COIL")]
    b = breakdown(ts, lambda t: t["setup"])
    assert b["COIL"]["n"] == 2 and b["IGNITION"]["win_rate"] == 1.0
    assert [score_bucket(x) for x in (None, 65, 70, 77, 85, 95)] == ["n/a", "<70", "70-74", "75-79", "80-89", "90+"]
    ms = int(datetime(2026, 7, 1, 13, 30, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)
    assert london_hour(ms) == "14"          # BST


def test_funding_timeline():
    legs = [{"ts": 2000, "qty": 4.0}]
    q = qty_timeline(10, legs)
    assert q(1999) == 10 and q(2000) == 6
    s = [(1000, 0.0001, 100.0), (3000, -0.0002, 0.0)]
    # long pays 0.0001*10*100 at t=1000, receives 0.0002*6*fallback(50) at t=3000
    assert funding_usd(s, 500, q, fallback_price=50.0) == pytest.approx(-0.1 + 0.06)
    assert funding_usd(s, 1000, q, 50.0) == pytest.approx(0.06)   # settlement at entry time not charged


@pytest.fixture
async def eng(cfg, tmp_path):
    data = copy.deepcopy(cfg.to_dict())
    data["app"]["data_dir"] = str(tmp_path)
    data["telegram"]["enabled"] = False
    e = Engine(Config(data, cfg.path))
    yield e
    await e.rest.close()
    await e.tg.close()


async def test_bedtime_sessions(eng):
    tr_ = eng.trading
    at = lambda h, m, d=10: datetime(2026, 10, d, h, m, tzinfo=LON)
    assert tr_.bedtime_session(at(23, 29)) is None
    assert tr_.bedtime_session(at(23, 30)) == "2026-10-10"
    assert tr_.bedtime_session(at(2, 0, 11)) == "2026-10-10"      # after midnight: same night
    assert tr_.bedtime_session(at(6, 0, 11)) is None               # reminders stop at 06:00
    assert tr_.snooze_key(at(20, 0)) == "2026-10-10"               # /snooze before bedtime = tonight


async def test_backup_written_once_and_rotated(eng):
    bdir = eng.cfg.data_dir / "backups"
    bdir.mkdir()
    for i in range(35):
        (bdir / f"scanner-2020-01-{i + 1:02d}.db").write_bytes(b"x") if i < 31 else \
            (bdir / f"scanner-2020-02-{i - 30:02d}.db").write_bytes(b"x")
    p = eng.trading.backup_now()
    assert p is not None and p.exists() and p.stat().st_size > 0
    assert eng.trading.backup_now() is None                         # once per day
    assert len(list(bdir.glob("scanner-*.db"))) == 30               # keep 30
