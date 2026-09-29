"""Dashboard: pages, API, security guards, settings persistence, login when not on localhost."""
import copy
import shutil

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core.config import Config
from core.engine import Engine
from web.server import WebHub, create_app
from web import settings as st

LOCAL = {"host": "localhost:8000"}


@pytest.fixture
def make(cfg, tmp_path, monkeypatch):
    engines = []

    def _make(bind_host="127.0.0.1", password=None):
        cfg_file = tmp_path / "config.yaml"
        shutil.copy(cfg.path, cfg_file)
        data = copy.deepcopy(cfg.to_dict())
        data["app"]["data_dir"] = str(tmp_path)
        data["telegram"]["enabled"] = False
        data["dashboard"]["bind_host"] = bind_host
        monkeypatch.setenv("DASHBOARD_PASSWORD", password or "")
        c = Config(data, cfg_file)
        eng = Engine(c)
        engines.append(eng)
        return eng, TestClient(create_app(eng, WebHub()), base_url="http://localhost:8000")

    yield _make


def test_pages_and_state(make):
    eng, c = make()
    for path in ("/", "/signals", "/settings", "/signal/S-0001", "/symbol/SOLUSDT"):
        r = c.get(path, headers=LOCAL)
        assert r.status_code == 200 and "Ignition &amp; Coil" in r.text, path
        assert "default-src 'self'" in r.headers["content-security-policy"]
    s = c.get("/api/state", headers=LOCAL).json()
    assert s["type"] == "state" and "header" in s and s["rows"] == []
    assert c.get("/static/vendor/lightweight-charts.standalone.production.js", headers=LOCAL).status_code == 200
    assert c.get("/api/signal/S-9999", headers=LOCAL).status_code == 404
    assert c.get("/api/signal/../x", headers=LOCAL).status_code in (400, 404)
    assert c.get("/api/chart/bad!sym", headers=LOCAL).status_code in (400, 404)


def test_signal_detail_roundtrip(make):
    eng, c = make()
    sid = eng.db.insert_signal(symbol="SOLUSDT", setup="IGNITION", bar_close_ms=1, score=81, regime="RISK_ON",
                               session="LONDON", config_hash="x", data={"plan": {"tp1": 1.0}, "tags": []})
    d = c.get(f"/api/signal/{sid}", headers=LOCAL).json()
    assert d["signal_id"] == sid and d["plan"]["tp1"] == 1.0 and "WEEX:SOLUSDT.P" in d["tv_link"]
    assert c.get("/api/signals", headers=LOCAL).json()[0]["signal_id"] == sid


def test_host_and_origin_guards(make):
    _, c = make()
    assert c.get("/api/state", headers={"host": "evil.example"}).status_code == 400      # DNS rebinding
    body = {"values": {}}
    assert c.post("/api/settings", json=body, headers={**LOCAL, "origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/settings", content="x=1", headers={**LOCAL, "content-type": "application/x-www-form-urlencoded"}).status_code == 415
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers={**LOCAL, "origin": "http://evil.example"}) as ws:
            ws.receive_text()
    with c.websocket_connect("/ws", headers={**LOCAL, "origin": "http://localhost:8000"}) as ws:
        assert '"type": "state"' in ws.receive_text()


def test_settings_save_keeps_comments_and_types(make):
    eng, c = make()
    old_margin = eng.cfg.trade.margin_usd
    vals = {"margin_usd": 25, "leverage": 40, "notional_usd": 0, "maker_fee": 0.0002, "taker_fee": 0.0006,
            "bedtime_reminder": "23:45"}
    r = c.post("/api/settings", json={"values": vals, "edited": "margin"},
               headers={**LOCAL, "origin": "http://localhost:8000"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["values"]["notional_usd"] == 1000 and out["values"]["leverage"] == 40
    assert out["changes"]["trade.margin_usd"] == [old_margin, 25]
    text = eng.cfg.path.read_text(encoding="utf-8")
    assert "# Ignition & Coil" in text and "WEEX public limit" in text      # comments preserved
    reloaded = yaml.safe_load(text)                                          # PyYAML = what the app uses
    assert reloaded["reminders"]["bedtime_reminder"] == "23:45"              # still a string, not 1425
    assert reloaded["trade"]["margin_usd"] == 25 and reloaded["trade"]["taker_fee"] == 0.0006
    assert eng.cfg.trade.margin_usd == 25                                    # live config updated
    assert eng.db.config_versions(5)[0].source == "settings"
    bad = c.post("/api/settings", json={"values": {**vals, "bedtime_reminder": "25:00"}},
                 headers={**LOCAL, "origin": "http://localhost:8000"})
    assert bad.status_code == 400


def test_settings_validation_linking():
    base = {"margin_usd": 20, "leverage": 50, "notional_usd": 1500, "maker_fee": 0.0002, "taker_fee": 0.0008,
            "bedtime_reminder": "23:30"}
    assert st.validate(base, "notional")["margin_usd"] == 30
    assert st.validate(base, "leverage")["notional_usd"] == 1000
    for bad in ({"leverage": 0}, {"leverage": 500}, {"taker_fee": 0.01}, {"margin_usd": "x"}):
        with pytest.raises(st.SettingsError):
            st.validate({**base, **bad})


def test_login_required_beyond_localhost(make):
    with pytest.raises(RuntimeError):
        make(bind_host="0.0.0.0", password="")
    _, c = make(bind_host="0.0.0.0", password="s3cret-pw")
    assert c.get("/api/state", headers=LOCAL).status_code == 401
    r = c.get("/", headers=LOCAL, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    hdr = {**LOCAL, "origin": "http://localhost:8000"}
    assert c.post("/login", json={"password": "nope"}, headers=hdr).status_code == 401
    ok = c.post("/login", json={"password": "s3cret-pw"}, headers=hdr)
    assert ok.status_code == 200 and "httponly" in ok.headers["set-cookie"].lower()
    assert c.get("/api/state", headers=LOCAL).status_code == 200
