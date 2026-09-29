"""Paper book + manual journal end to end, against a real engine with a fake WEEX client."""
import copy

import pytest
from sqlalchemy import select

from core.clock import TF_MS, floor_tf
from core.config import Config
from core.engine import Engine
from data.trade_db import ManualTradeEventRow, PaperTradeRow
from exchange.models import Bar
from journal.service import JournalError
from plan.liquidation import parse_brackets
from signals.engine import SignalEngine
from tests.test_setups import _eval

SYM = "TSTUSDT"
M5 = TF_MS["5m"]
BRK = parse_brackets([{"bracket": 1, "notionalFloor": "0", "notionalCap": "2000000", "initialLeverage": 300,
                       "maintMarginRatio": "0.002"}])


def bars(start_ms, n, step, path):
    """path(i) -> (o, h, l, c) for bar i."""
    return [Bar(start_ms + i * step, *path(i), 10, 1000, 5, 500, 1, start_ms + (i + 1) * step) for i in range(n)]


class FakeRest:
    """Minimal async stand-in for WeexRest, driven by a price path function of time."""

    def __init__(self, eng, price_at):
        self.eng, self.price_at = eng, price_at
        self.funding = []

    def _series(self, tf, limit, end=None):
        step = TF_MS[tf]
        end = floor_tf(end or self.eng.clock.now_ms(), tf)
        start = end - limit * step

        def path(i):
            t = start + i * step
            o, c = self.price_at(t), self.price_at(t + step - 1)
            lo, hi = min(o, c), max(o, c)
            for k in range(1, 5):   # sample inside the bar for wicks
                v = self.price_at(t + k * step // 5)
                lo, hi = min(lo, v), max(hi, v)
            return o, hi, lo, c
        return bars(start, limit, step, path)

    async def klines(self, sym, tf, limit):
        return self._series(tf, min(limit, 1000))

    async def mark_klines(self, sym, tf, limit):
        return self._series(tf, min(limit, 1000))

    async def history_klines(self, sym, tf, start, end, price_type="LAST"):
        step = TF_MS[tf]
        n = (floor_tf(end, tf) - floor_tf(start, tf)) // step + 1
        return [b for b in self._series(tf, n, floor_tf(end, tf) + step) if start <= b.t <= end]

    async def funding_history(self, sym, start, end):
        return [f for f in self.funding if start < f["fundingTime"] <= end]

    async def close(self):
        pass


@pytest.fixture
async def eng(cfg, tmp_path):
    data = copy.deepcopy(cfg.to_dict())
    data["app"]["data_dir"] = str(tmp_path)
    data["telegram"]["enabled"] = False
    data["trade"]["stop_trigger_price"] = "last"
    e = Engine(Config(data, cfg.path))
    await e.rest.close()
    e.universe.exchange_symbols = {SYM}
    e.universe.meta = {SYM: {"pricePrecision": 4}}
    e.brackets[SYM] = BRK
    e.rest = FakeRest(e, lambda t: 100.0)
    e.funding.rest = e.rest
    yield e
    await e.tg.close()


def alerts(e, event=None):
    from data.db import AlertLog
    with e.db.session() as s:
        q = select(AlertLog)
        return [a for a in s.execute(q).scalars() if event is None or a.event == event]


# ---------------------------------------------------------------- journal
async def test_create_trade_tracking_message_and_prefill(eng):
    t = await eng.journal.create({"symbol": SYM, "entry_price": 100.0, "margin": 20, "leverage": 50,
                                  "stop_policy": "L"})
    assert t["trade_ref"] == "M-0001" and t["status"] == "OPEN" and t["source"] == "manual_own"
    assert t["notional"] == 1000 and t["qty"] == pytest.approx(10)
    assert t["liq_price"] == pytest.approx(100 * 0.98 / 0.998, abs=1e-4)
    assert t["current_stop"] >= t["liq_price"] * 1.0015 - 1e-9
    assert t["tp1"] == pytest.approx(103.0)
    msgs = alerts(eng, "tracking")
    assert len(msgs) == 1 and "Set your SL on WEEX at" in msgs[0].text
    with pytest.raises(JournalError):
        await eng.journal.create({"symbol": SYM, "entry_price": 100, "margin": 20, "leverage": 50,
                                  "stop_policy": "custom", "stop_price": 101})     # stop above entry


async def test_tp1_alert_stop_hit_needs_confirmation_then_close(eng):
    t = await eng.journal.create({"symbol": SYM, "entry_price": 100.0, "margin": 20, "leverage": 50,
                                  "stop_policy": "custom", "stop_price": 99.0})
    now = eng.clock.now_ms()
    await eng.journal.on_sample(SYM, now + 1000, 100.5, 103.2, 103.0, None)
    tp1 = [a for a in alerts(eng) if "TP1 reached" in a.text]
    assert tp1 and "Close 40% at ~103" in tp1[0].text and "breakeven + fees" in tp1[0].text
    d = eng.journal.detail_by_ref(t["trade_ref"])
    assert any("TP1 reached" in p for p in d["pending"])            # you haven't logged the partial yet
    # price falls through YOUR logged stop (99): flagged until you log the exit
    await eng.journal.on_sample(SYM, now + 2000, 98.9, 99.5, 99.0, None)
    d = eng.journal.detail_by_ref(t["trade_ref"])
    assert d["status"] == "NEEDS_CONFIRMATION"
    assert any("Price hit your SL level" in a.text for a in alerts(eng))
    d = await eng.journal.log_exit(t["trade_ref"], {"price": 98.95}, full=True)
    assert d["status"] == "CLOSED"
    fee = 0.0008
    expected = 10 * (98.95 - 100) - 10 * 100 * fee - 10 * 98.95 * fee
    assert d["net_pnl"] == pytest.approx(expected)
    assert d["r_multiple"] == pytest.approx(expected / d["risk_usd"])
    assert d["plan_pnl"] is not None and d["adherence_usd"] is not None


async def test_partial_edit_void_audit_trail(eng):
    t = await eng.journal.create({"symbol": SYM, "entry_price": 100.0, "margin": 20, "leverage": 50,
                                  "stop_policy": "custom", "stop_price": 98.5})
    ref = t["trade_ref"]
    d = await eng.journal.log_exit(ref, {"price": 103.0, "pct": 40, "order": "limit"}, full=False)
    assert d["qty_open"] == pytest.approx(6) and d["status"] == "OPEN"
    d = await eng.journal.log_stop(ref, {"price": 100.3})
    assert d["current_stop"] == 100.3
    part = next(e for e in d["audit"] if e["kind"] == "PARTIAL_CLOSE")
    d = await eng.journal.edit_event(ref, {"event_id": part["id"], "price": 103.1})
    assert d["legs"][0]["price"] == 103.1
    d = await eng.journal.log_exit(ref, {"price": 104.0}, full=True)
    assert d["status"] == "CLOSED"
    close = next(e for e in d["audit"] if e["kind"] == "CLOSE")
    d = await eng.journal.edit_event(ref, {"event_id": close["id"], "void": True})
    assert d["status"] == "OPEN" and d["qty_open"] == pytest.approx(6)       # reopened by the void
    with eng.db.session() as s:
        kinds = [r.kind for r in s.execute(select(ManualTradeEventRow)).scalars()]
    # append-only: nothing removed, corrections added
    assert kinds == ["OPEN", "PARTIAL_CLOSE", "STOP_MOVE", "EDIT", "CLOSE", "VOID"]


async def test_edit_trade_recomputes_and_audits(eng):
    t = await eng.journal.create({"symbol": SYM, "entry_price": 100.0, "margin": 20, "leverage": 50,
                                  "stop_policy": "custom", "stop_price": 98.5})
    d = await eng.journal.edit_trade(t["trade_ref"], {"entry_price": 101.0, "leverage": 25, "edited": "leverage"})
    assert d["notional"] == 500 and d["qty"] == pytest.approx(500 / 101)
    ev = [e for e in d["audit"] if e["kind"] == "EDIT"][0]
    assert ev["old"]["entry_price"] == 100.0 and ev["new"]["entry_price"] == 101.0


async def test_late_logging_replays_the_plan(eng):
    now = eng.clock.now_ms()
    entry_ms = floor_tf(now, "5m") - 12 * M5          # an hour ago
    # price: 100 at entry, rallies through TP1 (103) 30 minutes later, then holds ~103.5
    eng.rest.price_at = lambda t: 100.0 if t < entry_ms + 30 * 60_000 else 103.5
    t = await eng.journal.create({"symbol": SYM, "entry_price": 100.0, "margin": 20, "leverage": 50,
                                  "stop_policy": "custom", "stop_price": 98.5,
                                  "entry_time": entry_ms})
    d = eng.journal.detail_by_ref(t["trade_ref"])
    kinds = [e["kind"] for e in d["plan_events"]]
    assert "TP1" in kinds and "STOP_MOVE" in kinds
    assert any(p.startswith("TP1 reached at") for p in d["pending"])
    assert any(p.startswith("move SL to") for p in d["pending"])
    msg = alerts(eng, "tracking")[-1].text
    assert "Logged late - the plan so far:" in msg and "TP1 reached at" in msg and "SL should now be at" in msg


async def test_restart_catch_up_flags_offline_stop_hit(eng, cfg):
    t = await eng.journal.create({"symbol": SYM, "entry_price": 100.0, "margin": 20, "leverage": 50,
                                  "stop_policy": "custom", "stop_price": 99.0})
    # while "offline" the price dropped through the logged stop
    eng.journal.shadows.clear()
    now = eng.clock.now_ms()
    with eng.db.session() as s:          # pretend the trade was entered 40 min ago
        from data.trade_db import ManualTradeRow
        row = s.get(ManualTradeRow, t["id"])
        row.entry_ms = floor_tf(now, "5m") - 8 * M5
        st = eng.journal._new_shadow(row)
        row.shadow = st.to_dict()
        s.commit()
    eng.rest.price_at = lambda x: 98.0
    lines = await eng.journal.load_and_catch_up()
    d = eng.journal.detail_by_ref(t["trade_ref"])
    assert d["status"] == "NEEDS_CONFIRMATION" and "price hit your SL" in d["needs_confirmation"]
    assert lines and "NEEDS CONFIRMATION" in lines[0]


async def test_close_all(eng):
    a = await eng.journal.create({"symbol": SYM, "entry_price": 100, "margin": 20, "leverage": 50, "stop_policy": "L"})
    b = await eng.journal.create({"symbol": SYM, "entry_price": 101, "margin": 20, "leverage": 50, "stop_policy": "L"})
    r = await eng.journal.close_all([{"trade_ref": a["trade_ref"], "price": 101}, {"trade_ref": b["trade_ref"], "price": 101}])
    assert r["closed"] == [a["trade_ref"], b["trade_ref"]] and not r["errors"]
    assert eng.journal.list()["open"] == []


# ---------------------------------------------------------------- paper
def signal_record(cfg):
    _, sigs, _ = _eval(SignalEngine(cfg), cfg, floor_tf(1_790_000_000_000, "5m"))
    rec = sigs[0].to_record()
    rec["signal_id"] = "S-0001"
    rec["suppressed_reason"] = "risk_off"
    return rec


async def test_paper_opens_both_policies_and_closes_with_funding(eng):
    rec = signal_record(eng.cfg)
    ids = eng.paper.open_for_signal(rec)
    assert len(ids) == (2 if rec["plan"]["stop_s"]["price"] is not None else 1)
    assert eng.paper.open_for_signal(rec) == []                     # idempotent
    t0 = rec["bar_close_ms"]
    plan = rec["plan"]
    eng.rest.funding = [{"fundingTime": t0 + 60_000, "fundingRate": "0.0001", "markPrice": "102.5"}]
    await eng.paper.on_sample(SYM.replace(SYM, rec["symbol"]), t0 + 30_000, plan["ref_entry"], plan["tp1"] + 0.01,
                              plan["tp1"], None)
    closed = await eng.paper.on_sample(rec["symbol"], t0 + 90_000, 90.0, plan["tp1"], 95.0, None)
    assert closed and all(c["exit_reason"] == "STOP" for c in closed)
    with eng.db.session() as s:
        rows = list(s.execute(select(PaperTradeRow)).scalars())
    for r in rows:
        assert r.status == "CLOSED" and r.suppressed_reason == "risk_off"
        assert [l["reason"] for l in r.legs] == ["TP1", "STOP"]
        assert r.funding_usd == pytest.approx(-0.0001 * 0.6 * r.qty * 102.5)   # 60% still open at settlement
        assert r.tp1_ms == t0 + 30_000 and r.mfe_pct > 2.9


async def test_csv_export(eng, tmp_path):
    from tools.export_trades import export
    t = await eng.journal.create({"symbol": SYM, "entry_price": 100, "margin": 20, "leverage": 50, "stop_policy": "L"})
    await eng.journal.log_exit(t["trade_ref"], {"price": 102.5}, full=True)
    today = eng.clock.fmt(eng.clock.now_ms(), with_date=True)[:10]
    paths = export(eng.cfg, today, today, "manual", tmp_path / "out")
    text = paths[0].read_text(encoding="utf-8").splitlines()
    assert len(text) == 2 and "M-0001" in text[1] and "CLOSED" in text[1] and "@ 102.5" in text[1]
