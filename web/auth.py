"""Dashboard access control.

- Bound to localhost (default): no login, but still protected against other websites in your
  browser: Host header must be a known name (DNS-rebinding), POSTs must be JSON from the same
  origin, and the WebSocket origin must match (cross-site WebSocket hijacking).
- Bound to anything else: login required (DASHBOARD_PASSWORD from .env), signed session cookie.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
COOKIE = "ic_session"


def login_required(bind_host: str) -> bool:
    return bind_host not in LOCAL_HOSTS


def load_secret(data_dir: Path) -> bytes:
    p = data_dir / "session_secret"
    if not p.exists():
        p.write_bytes(secrets.token_bytes(32))
    return p.read_bytes()


def make_token(secret: bytes, days: float) -> str:
    exp = str(int(time.time() + days * 86400))
    sig = hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_token(secret: bytes, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    good = hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()


def check_password(given: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


def allowed_hosts(cfg) -> set[str]:
    d = cfg.dashboard
    hosts = set(LOCAL_HOSTS) | {d.bind_host}
    base = urlparse(d.base_url).hostname
    if base:
        hosts.add(base)
    hosts |= set(d.extra_allowed_hosts or [])
    return hosts


def host_ok(host_header: str | None, hosts: set[str]) -> bool:
    if not host_header:
        return False
    h = host_header.strip().lower()
    if h.startswith("["):                      # [::1]:8000
        h = h[1:h.find("]")] if "]" in h else h
    else:
        h = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return h in hosts


def same_origin(origin: str | None, host_header: str | None) -> bool:
    """True if Origin (when present) points at the host being served."""
    if origin is None:
        return True           # non-browser clients (curl) send no Origin
    try:
        return urlparse(origin).netloc.lower() == (host_header or "").lower()
    except ValueError:
        return False
