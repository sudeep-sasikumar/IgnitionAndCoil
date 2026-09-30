"""Dashboard server (spec §12): FastAPI + one WebSocket, single-page frontend, all assets local."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import time
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from core.config import load_env
from data.db import clean_json
from journal.service import JournalError
from stats import service as stats_service
from web import auth, settings, views

log = logging.getLogger("web")

STATIC = Path(__file__).resolve().parent / "static"
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,30}$")
SIGNAL_RE = re.compile(r"^S-\d{1,9}$")
PAGES = ("/", "/settings", "/signals", "/trades", "/trades/new", "/trades/close-all", "/stats", "/highs", "/research")
TRADE_RE = re.compile(r"^M-\d{1,9}$")
PUBLIC = ("/login", "/static/app.css", "/static/favicon.svg", "/static/login.js")
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class WebHub:
    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def broadcast(self, msg: dict) -> None:
        if not self.clients:
            return
        text = json.dumps(clean_json(msg))
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001 - client went away
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


def create_app(eng, hub: WebHub) -> FastAPI:
    cfg = eng.cfg
    d = cfg.dashboard
    need_login = auth.login_required(d.bind_host)
    password = load_env()["DASHBOARD_PASSWORD"]
    if need_login and not password:
        raise RuntimeError("dashboard.bind_host is not localhost, so a login is required: "
                           "set DASHBOARD_PASSWORD in .env")
    secret = auth.load_secret(cfg.data_dir)
    hosts = auth.allowed_hosts(cfg)
    failures: dict[str, list[float]] = {}

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def authed(request_cookies) -> bool:
        return (not need_login) or auth.valid_token(secret, request_cookies.get(auth.COOKIE))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host")
        if not auth.host_ok(host, hosts):
            return PlainTextResponse("Unknown host", status_code=400)
        path = request.url.path
        if request.method not in ("GET", "HEAD"):
            if not auth.same_origin(request.headers.get("origin"), host):
                return PlainTextResponse("Cross-origin request refused", status_code=403)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return PlainTextResponse("JSON required", status_code=415)
        if not (path in PUBLIC or authed(request.cookies)):
            if path.startswith("/api"):
                return JSONResponse({"error": "login required"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        resp: Response = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["X-Frame-Options"] = "DENY"
        if path.startswith("/api"):
            resp.headers["Cache-Control"] = "no-store"
        elif path.startswith("/static") and "Cache-Control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-cache"   # revalidate: a redeploy shows up on the next reload
        return resp

    # ---- pages -------------------------------------------------------------
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    for p in PAGES:
        app.add_api_route(p, index, methods=["GET"], include_in_schema=False)

    @app.get("/signal/{signal_id}", include_in_schema=False)
    async def signal_page(signal_id: str):
        return index()

    @app.get("/trade/{trade_ref}", include_in_schema=False)
    async def trade_page(trade_ref: str):
        return index()

    @app.get("/symbol/{symbol}", include_in_schema=False)
    async def symbol_page(symbol: str):
        return index()

    @app.get("/login", include_in_schema=False)
    async def login_page():
        if not need_login:
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC / "login.html")

    @app.post("/login", include_in_schema=False)
    async def login(request: Request):
        ip = request.client.host if request.client else "?"
        recent = [t for t in failures.get(ip, []) if time.time() - t < 900]
        if len(recent) >= 10:
            return JSONResponse({"error": "too many attempts, try again in 15 minutes"}, status_code=429)
        body = await request.json()
        if not auth.check_password(str(body.get("password", "")), password):
            failures[ip] = recent + [time.time()]
            await asyncio.sleep(1.0)
            return JSONResponse({"error": "wrong password"}, status_code=401)
        failures.pop(ip, None)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(auth.COOKIE, auth.make_token(secret, d.session_days), max_age=int(d.session_days * 86400),
                        httponly=True, samesite="strict", secure=bool(d.cookie_secure))
        return resp

    @app.post("/logout", include_in_schema=False)
    async def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(auth.COOKIE)
        return resp

    # ---- API -----------------------------------------------------------------
    @app.get("/api/state")
    async def api_state():
        return views.state(eng)

    @app.get("/api/signals")
    async def api_signals(limit: int = 200):
        rows = eng.db.recent_signals(0)
        return [views.signal_summary(s) for s in reversed(rows)][: max(1, min(limit, 1000))]

    @app.get("/api/signal/{signal_id}")
    async def api_signal(signal_id: str):
        if not SIGNAL_RE.match(signal_id):
            return JSONResponse({"error": "bad signal id"}, status_code=400)
        d = views.signal_detail(eng, signal_id)
        return d if d else JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/api/symbol/{symbol}")
    async def api_symbol(symbol: str):
        if not SYMBOL_RE.match(symbol):
            return JSONResponse({"error": "bad symbol"}, status_code=400)
        d = views.symbol_detail(eng, symbol)
        return d if d else JSONResponse({"error": "not in the current scan"}, status_code=404)

    @app.get("/api/chart/{symbol}")
    async def api_chart(symbol: str):
        if not SYMBOL_RE.match(symbol):
            return JSONResponse({"error": "bad symbol"}, status_code=400)
        if not eng.store.has(symbol) and symbol not in eng.universe.exchange_symbols:
            return JSONResponse({"error": "unknown symbol"}, status_code=404)
        return await views.chart_data(eng, symbol)

    @app.get("/api/settings")
    async def api_settings():
        return clean_json({
            "values": settings.current(cfg), "config_hash": cfg.hash,
            "config_yaml": Path(cfg.path).read_text(encoding="utf-8"),
            "versions": [{"ts": v.ts, "hash": v.config_hash, "source": v.source, "changes": v.changes}
                         for v in eng.db.config_versions(10)],
            "login_required": need_login,
        })

    @app.post("/api/settings")
    async def api_settings_save(request: Request):
        body = await request.json()
        try:
            values = settings.validate(body.get("values", {}), body.get("edited", "margin"))
        except settings.SettingsError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        changes = settings.save(cfg, values)
        eng.db.record_config(cfg.hash, cfg.to_dict(), "settings", changes)
        log.info("settings saved: %s", changes)
        return {"ok": True, "values": settings.current(cfg), "changes": changes, "config_hash": cfg.hash}

    # ---- journal (records what you did on WEEX; never touches WEEX) ---------------
    async def _journal(call):
        try:
            out = await call()
        except JournalError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await eng.push_state()
        return out

    def _ref(trade_ref: str) -> str:
        if not TRADE_RE.match(trade_ref):
            raise JournalError("bad trade id")
        return trade_ref

    @app.get("/api/trades")
    async def api_trades():
        return eng.journal.list()

    @app.get("/api/trade/{trade_ref}")
    async def api_trade(trade_ref: str):
        try:
            return eng.journal.detail_by_ref(_ref(trade_ref))
        except JournalError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

    @app.post("/api/trades/preview")
    async def api_trade_preview(request: Request):
        body = await request.json()
        try:
            return await eng.journal.preview(body)
        except JournalError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/api/trades")
    async def api_trade_create(request: Request):
        body = await request.json()

        async def run():
            out = await eng.journal.create(body)
            await eng.ensure_tracked(out["symbol"])
            return out
        return await _journal(run)

    @app.post("/api/trades/close_all")
    async def api_close_all(request: Request):
        body = await request.json()
        return await _journal(lambda: eng.journal.close_all(list(body.get("items") or [])))

    @app.post("/api/trade/{trade_ref}/{action}")
    async def api_trade_action(trade_ref: str, action: str, request: Request):
        body = await request.json()
        j = eng.journal
        calls = {
            "close": lambda: j.log_exit(_ref(trade_ref), body, full=True),
            "partial": lambda: j.log_exit(_ref(trade_ref), body, full=False),
            "stop": lambda: j.log_stop(_ref(trade_ref), body),
            "edit": lambda: j.edit_trade(_ref(trade_ref), body),
            "event": lambda: j.edit_event(_ref(trade_ref), body),
            "confirm": lambda: j.confirm_open(_ref(trade_ref), body),
        }
        if action not in calls:
            return JSONResponse({"error": "unknown action"}, status_code=404)
        return await _journal(calls[action])

    @app.get("/api/paper")
    async def api_paper(signal_id: str | None = None, status: str | None = None, limit: int = 200):
        return views.paper_list(eng, signal_id, status, max(1, min(limit, 2000)))

    @app.get("/api/stats")
    async def api_stats(days: int = 0):
        since = 0 if days <= 0 else eng.clock.now_ms() - days * 86_400_000
        return stats_service.compute(eng.db, since)

    @app.get("/api/highs")
    async def api_highs():
        if eng.highs is None:
            return {"disabled": True}
        return await asyncio.to_thread(eng.highs.view)

    @app.get("/api/research")
    async def api_research():
        def load():
            base = Path(cfg.data_dir) / "research"
            reports = {}
            shipped = Path(__file__).resolve().parent.parent / "research"      # snapshots shipped with the code
            for name in ("history", "live", "candidates"):
                p = base / "reports" / f"{name}.json"
                if not p.exists() and name != "live":
                    p = shipped / ("history_report.json" if name == "history" else "candidates_report.json")
                if p.exists():
                    try:
                        reports[name] = json.loads(p.read_text(encoding="utf-8"))
                    except ValueError:
                        pass
            files = [{"name": f.name, "bytes": f.stat().st_size} for f in sorted((base / "snapshots").glob("*.csv.gz"))]
            market = [{"name": f.name, "bytes": f.stat().st_size} for f in sorted((base / "market").glob("market-*.npz"))]
            return {"reports": reports, "files": files, "market_files": market, "recording": eng.recorder is not None,
                    "market_recording": eng.market is not None}
        return await asyncio.to_thread(load)

    @app.get("/api/research/file/{name}")
    async def api_research_file(name: str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(\.[a-z])?\.csv\.gz", name):
            p, media = Path(cfg.data_dir) / "research" / "snapshots" / name, "application/gzip"
        elif re.fullmatch(r"market-\d{4}-\d{2}-\d{2}\.npz", name):
            p, media = Path(cfg.data_dir) / "research" / "market" / name, "application/octet-stream"
        else:
            return JSONResponse({"error": "bad file name"}, status_code=400)
        if not p.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(p, media_type=media, filename=name)

    @app.get("/api/symbols")
    async def api_symbols():
        return sorted(s for s in eng.universe.exchange_symbols
                      if (eng.universe.meta.get(s) or {}).get("contractType") in (None, "PERPETUAL"))

    # ---- websocket -----------------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        host = ws.headers.get("host")
        if not (auth.host_ok(host, hosts) and auth.same_origin(ws.headers.get("origin"), host)
                and authed(ws.cookies)):
            await ws.close(code=1008)
            return
        await ws.accept()
        hub.clients.add(ws)
        try:
            await ws.send_text(json.dumps(clean_json(views.state(eng))))
            while True:
                await ws.receive_text()   # we only push; keep the socket open
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(ws)

    return app


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


async def serve(eng, hub: WebHub) -> None:
    d = eng.cfg.dashboard
    try:
        app = create_app(eng, hub)
    except RuntimeError as e:
        print(f"DASHBOARD NOT STARTED: {e}")
        eng.incident("dashboard", str(e), severity="critical")
        return
    if not port_free(d.bind_host, d.port):
        msg = f"port {d.port} on {d.bind_host} is already in use (is the scanner already running?)"
        print(f"DASHBOARD NOT STARTED: {msg}")
        eng.incident("dashboard", msg, severity="critical")
        return
    server = uvicorn.Server(uvicorn.Config(app, host=d.bind_host, port=d.port, log_level="warning",
                                           access_log=False, lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():
        await asyncio.sleep(0.1)
    if server.started:
        print(f"Dashboard: {d.base_url}")
        if d.open_browser:
            webbrowser.open(d.base_url)
    await task


async def price_loop(eng, hub: WebHub) -> None:
    while True:
        await asyncio.sleep(eng.cfg.dashboard.price_push_s)
        if hub.clients:
            await hub.broadcast(views.prices(eng))
