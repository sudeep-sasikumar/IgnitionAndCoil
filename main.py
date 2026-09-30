"""Ignition & Coil scanner - single entry point.

    python main.py            run continuously
    python main.py --once     build universe, compute one scan, print, exit
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from urllib.parse import urlparse

from core.config import load_config
from core.engine import Engine
from core.logs import setup_logging


def apply_port_env(cfg) -> None:
    """PORT environment variable (dev preview, Docker) overrides dashboard.port. Localhost
    alert links follow it; a public base_url (e.g. a VPS domain) is left alone."""
    port = os.environ.get("PORT", "").strip()
    if not port:
        return
    d = cfg.to_dict()["dashboard"]
    d["port"] = int(port)
    u = urlparse(d["base_url"])
    if u.hostname in ("localhost", "127.0.0.1"):
        d["base_url"] = f"{u.scheme}://{u.hostname}:{port}"


def main() -> int:
    # Windows consoles/redirects default to cp1252, which cannot print the emoji in alert text.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Ignition & Coil crypto momentum scanner (signals only; never trades)")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    ap.add_argument("--once", action="store_true", help="run a single scan and exit")
    ap.add_argument("--no-browser", action="store_true", help="don't open the dashboard in the browser")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.no_browser:
        cfg.to_dict()["dashboard"]["open_browser"] = False
    apply_port_env(cfg)
    a = cfg.app
    setup_logging(cfg.data_dir / "logs", a.log_level, a.log_max_bytes, a.log_backups)
    print(f"Ignition & Coil - version {os.environ.get('APP_VERSION', 'dev')[:7]} - config {cfg.hash} - data in {cfg.data_dir}")
    try:
        asyncio.run(Engine(cfg).run(once=args.once))
    except KeyboardInterrupt:
        print("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
