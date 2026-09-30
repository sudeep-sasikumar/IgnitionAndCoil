"""Download the VPS's research recordings to this PC.

    .venv\\Scripts\\python.exe tools\\sync_research.py            (normally run daily by Task Scheduler)

Logs in to the dashboard (research.sync_url) with DASHBOARD_PASSWORD from this PC's .env, then
downloads every daily file this PC doesn't have yet into <data_dir>/research/vps/ and always
re-downloads today's and yesterday's (UTC) files, which the VPS is still writing. A file is
only replaced after the new copy passes a full gzip check, so an interrupted or mid-write
download never damages what you have. The password is never printed, logged or passed on a
command line. Log: <data_dir>/logs/research_sync.log.
"""
from __future__ import annotations

import argparse
import gzip
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_config, load_env  # noqa: E402

NAME = re.compile(r"\d{4}-\d{2}-\d{2}\.csv\.gz")
log = logging.getLogger("research_sync")


def gzip_ok(p: Path) -> bool:
    """Every gzip member complete and readable (the files are appended as many members)."""
    try:
        with gzip.open(p, "rb") as fh:
            while fh.read(1 << 20):
                pass
        return True
    except (OSError, EOFError):
        return False


def download(c: httpx.Client, name: str, dest: Path, tries: int = 3) -> int | None:
    """Fetch one file to a temp name, verify it, then swap it in. Returns bytes, or None."""
    tmp = dest.with_name(dest.name + ".part")
    for attempt in range(1, tries + 1):
        try:
            with c.stream("GET", f"/api/research/file/{name}") as r:
                if r.status_code != 200:
                    log.warning("%s: HTTP %s", name, r.status_code)
                    return None
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_bytes():
                        fh.write(chunk)
            if gzip_ok(tmp):
                os.replace(tmp, dest)
                return dest.stat().st_size
            log.info("%s: incomplete copy (the VPS was writing it), retry %d", name, attempt)
        except httpx.HTTPError as e:
            log.info("%s: %s, retry %d", name, type(e).__name__, attempt)
        time.sleep(10 * attempt)
    tmp.unlink(missing_ok=True)
    return None


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Download the VPS's research recordings")
    ap.add_argument("--url", default=str(cfg.research.sync_url), help="dashboard address (default research.sync_url)")
    ap.add_argument("--dest", default=str(Path(cfg.data_dir) / "research" / "vps"))
    ap.add_argument("--refresh-days", type=int, default=2, help="re-download the newest N UTC days (still being written)")
    a = ap.parse_args()

    logs = Path(cfg.data_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(logs / "research_sync.log", encoding="utf-8")]
    if sys.stdout is not None:                       # pythonw (Task Scheduler) has no console
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)   # no request lines in the log

    dest = Path(a.dest)
    dest.mkdir(parents=True, exist_ok=True)
    password = load_env().get("DASHBOARD_PASSWORD", "")
    if not password.strip():
        log.error("DASHBOARD_PASSWORD is not set in %s - add it there (the same one you log in with)", Path(cfg.path).parent / ".env")
        return 2
    today = datetime.now(timezone.utc).date()
    refresh = {(today - timedelta(days=k)).isoformat() for k in range(a.refresh_days)}
    got = kept = failed = 0
    with httpx.Client(base_url=a.url.rstrip("/"), timeout=120, follow_redirects=False) as c:
        r = c.post("/login", json={"password": password})
        password = ""                                # not needed any more
        if r.status_code != 200:
            try:
                why = r.json().get("error", "")
            except ValueError:
                why = ""
            log.error("login to %s failed: HTTP %s %s", a.url, r.status_code, why)
            return 3
        files = c.get("/api/research").json().get("files", [])
        for f in files:
            name = f["name"]
            if not NAME.fullmatch(name):
                continue
            local = dest / name
            fresh = name[:10] in refresh
            if local.exists() and not fresh and local.stat().st_size == f["bytes"]:
                kept += 1
                continue
            existed = local.exists()
            size = download(c, name, local)
            if size is None:
                failed += 1
                log.warning("%s: not downloaded (kept the previous copy, if any)", name)
            else:
                got += 1
                log.info("%s: %s (%.2f MB)", name, "refreshed" if existed else "downloaded", size / 1e6)
        c.post("/logout")
    log.info("done: %d downloaded/refreshed, %d already up to date, %d failed, %d on the VPS -> %s",
             got, kept, failed, len(files), dest)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
