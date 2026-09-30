"""Alert text, signal IDs, restart dedupe and suppression rules (Telegram not configured ->
messages go to the console, which is what these tests exercise)."""
import copy

import pytest

from core.config import Config
from core.engine import Engine
from data.db import Database
from notify import messages
from notify.telegram import Telegram
from signals.engine import SignalEngine
from tests.helpers import M5
from tests.test_setups import _eval


@pytest.fixture
def tmp_cfg(cfg, tmp_path):
    data = copy.deepcopy(cfg.to_dict())
    data["app"]["data_dir"] = str(tmp_path)
    data["telegram"]["enabled"] = False
    return Config(data, cfg.path)


def make_signal(cfg, t=1_000_000 * M5):
    _, sigs, _ = _eval(SignalEngine(cfg), cfg, t)
    assert sigs
    return sigs[0]


def test_entry_message_matches_spec_layout(tmp_cfg):
    sig = make_signal(tmp_cfg)
    rec = sig.to_record()
    rec["signal_id"] = "S-0412"
    text = messages.entry_message(rec, sig.bar_close_ms, tmp_cfg, 2)
    lines = text.splitlines()
    assert lines[0].startswith("🚀 IGNITION — TSTUSDT | score ") and lines[0].endswith("| RISK_ON | #S-0412")
    assert "Entry ~102.00 | Don't enter above 102.51 | Liq ~" in text and f"({tmp_cfg.trade.leverage:g}x, mark)" in text
    assert "Stop L " in text and "BE win rate" in text and "Stop S " in text
    mine = tmp_cfg.trade.default_stop_policy
    assert f"Stop {mine} " in [l[:7] for l in lines if "◀ your stop" in l][0]
    assert "TP1 105.06 (+3%, close 40%)" in text
    assert "Runner 30%: chandelier / 15m swing-low trail, stop only moves up" in text
    assert "RVOL 6.0x | RS +2.5% | OI +4.0% 1h | Taker 0.64" in text
    assert "Score: RVOL" in text
    assert "tradingview.com/chart/?symbol=WEEX:TSTUSDT.P" in text
    assert "http://localhost:8000/signal/S-0412" in text


def test_tv_fallback_from_missing_file(tmp_cfg, tmp_path):
    (tmp_path / "tv_missing.json").write_text('["BPUSDT"]')
    assert messages.tv_symbol("BPUSDT", tmp_cfg) == "BINANCE:BPUSDT.P"
    assert messages.tv_symbol("SOLUSDT", tmp_cfg) == "WEEX:SOLUSDT.P"


def test_signal_ids_and_restart_dedupe(tmp_path):
    db = Database(f"sqlite:///{tmp_path.as_posix()}/t.db")
    kw = dict(symbol="A", setup="IGNITION", bar_close_ms=1, score=80, regime="RISK_ON", session="LONDON",
              config_hash="x", data={"v": float("nan")})
    assert db.insert_signal(**kw) == "S-0001"
    assert db.insert_signal(**kw) is None                      # same bar again (restart) -> not duplicated
    assert db.insert_signal(**{**kw, "symbol": "B"}) == "S-0002"
    assert db.get_signal("S-0001").data == {"v": None}         # NaN cleaned for JSON


async def test_telegram_send_is_deduplicated(tmp_path, cfg, capsys):
    db = Database(f"sqlite:///{tmp_path.as_posix()}/t.db")
    tg = Telegram(cfg, "", "", db)                            # not configured -> console
    await tg.send("hello", dedupe_key="S-0001:entry", event="entry")
    await tg.send("hello", dedupe_key="S-0001:entry", event="entry")
    assert capsys.readouterr().out.count("hello") == 1
    assert db.count_alerts_since("entry", 0) == 1
    await tg.close()


async def _publish(engine, sig):
    await engine.publish_signals([sig])
    return engine.db.get_signal(sig.signal_id)


async def test_suppression_rules(tmp_cfg):
    eng = Engine(tmp_cfg)
    try:
        row = await _publish(eng, make_signal(tmp_cfg, 1_000_000 * M5))
        assert row.alert_status == "console" and row.suppressed_reason is None

        eng.db.set_state("signals_paused", True)
        row = await _publish(eng, make_signal(tmp_cfg, 1_000_100 * M5))
        assert (row.alert_status, row.suppressed_reason) == ("suppressed", "paused")
        eng.db.set_state("signals_paused", False)

        sig_cfg = tmp_cfg.to_dict()["signals"]
        before = sig_cfg.get("suppress_longs_in_risk_off")
        try:
            sig_cfg["suppress_longs_in_risk_off"] = True          # RISK_OFF longs held back
            s = make_signal(tmp_cfg, 1_000_200 * M5)
            s.tags.append("RISK_OFF")
            assert (await _publish(eng, s)).suppressed_reason == "risk_off"
            sig_cfg["suppress_longs_in_risk_off"] = False         # ... or alerted (config since 2026-09-30)
            s = make_signal(tmp_cfg, 1_000_250 * M5)
            s.tags.append("RISK_OFF")
            assert (await _publish(eng, s)).suppressed_reason is None
        finally:
            sig_cfg["suppress_longs_in_risk_off"] = before

        s = make_signal(tmp_cfg, 1_000_300 * M5)
        s.tags.append("BLACKOUT:CPI")
        assert (await _publish(eng, s)).suppressed_reason == "blackout"

        s = make_signal(tmp_cfg, 1_000_350 * M5)
        s.plan.stop_s.price = None                  # structural stop doesn't fit: your model can't trade it
        if tmp_cfg.trade.default_stop_policy == "S" and tmp_cfg.trade.require_default_policy:
            assert (await _publish(eng, s)).suppressed_reason == "default_policy_na"

        for i in range(6):
            eng.db.log_alert(dedupe_key=f"x{i}", event="entry", status="sent", text="x")
        assert (await _publish(eng, make_signal(tmp_cfg, 1_000_400 * M5))).suppressed_reason == "hourly_cap"
    finally:
        await eng.rest.close()
        await eng.tg.close()


async def test_commands(tmp_cfg):
    eng = Engine(tmp_cfg)
    try:
        assert "paused" in (await eng.handle_command("/pause", "")).lower() and eng.paused
        await eng.handle_command("/resume", "")
        assert not eng.paused
        await eng.handle_command("/mute", "")
        assert eng.watch_muted
        assert await eng.handle_command("/open", "") == "No open logged trades."
        assert "muted" in (await eng.handle_command("/mutepaper", "")).lower()
        assert "snoozed" in (await eng.handle_command("/snooze", "")).lower()
    finally:
        await eng.rest.close()
        await eng.tg.close()
