"""Startup environment checks (spec §14)."""
from __future__ import annotations

import re
import subprocess
import sys

# GUID aliases understood by powercfg on every Windows language.
_SLEEP_QUERY = ["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"]


def windows_sleep_timeouts() -> tuple[int, int] | None:
    """Returns (AC, DC) sleep-after seconds for the active power plan (0 = never), or None."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(_SLEEP_QUERY, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # The last two hex values on the page are the current AC and DC indexes (language-independent).
    vals = re.findall(r":\s*0x([0-9a-fA-F]{8})\s*$", out, flags=re.M)
    if len(vals) < 2:
        return None
    return int(vals[-2], 16), int(vals[-1], 16)


def sleep_warning() -> str | None:
    t = windows_sleep_timeouts()
    if t is None:
        return None
    ac, _dc = t
    if ac:
        return (f"Windows is set to sleep after {ac // 60} min on mains power. While asleep, alerts and "
                f"trade tracking stop. See README 'Keep the PC awake'.")
    return None
