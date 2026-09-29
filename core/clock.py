"""Exchange-synced clock and timeframe helpers. UTC internally, London for display."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


class Clock:
    """Local clock corrected by the measured offset to WEEX server time."""

    def __init__(self, display_tz: str = "Europe/London"):
        self.offset_ms = 0
        self.tz = ZoneInfo(display_tz)

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.offset_ms

    def set_offset(self, server_ms: int, local_before_ms: int, local_after_ms: int) -> float:
        """Record offset (server - local) using the request midpoint; returns drift in seconds."""
        mid = (local_before_ms + local_after_ms) // 2
        self.offset_ms = server_ms - mid
        return self.offset_ms / 1000.0

    def fmt(self, ms: int | None, with_date: bool = False) -> str:
        if ms is None:
            return "-"
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(self.tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


def parse_local(value, tz_name: str) -> int:
    """A time from the dashboard: epoch ms, or 'YYYY-MM-DDTHH:MM[:SS]' in the display timezone
    (London), converted to UTC ms (DST handled by zoneinfo)."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=ZoneInfo(tz_name))
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"bad time '{value}' (expected YYYY-MM-DDTHH:MM, London time)")


def floor_tf(ms: int, tf: str) -> int:
    step = TF_MS[tf]
    return ms - ms % step


def next_bar_close(last_processed_close: int, now_ms: int, tf: str = "5m") -> int:
    """Close time of the next bar to process. If bars were missed (slow startup straddling a
    boundary, PC sleep), jump straight to the newest closed bar instead of the next one."""
    step = TF_MS[tf]
    newest_closed = floor_tf(now_ms, tf)
    if not last_processed_close:
        return newest_closed + step
    return max(last_processed_close + step, newest_closed)


def last_closed_open(now_ms: int, tf: str) -> int:
    """Open time of the most recent bar that has fully closed at now_ms."""
    return floor_tf(now_ms, tf) - TF_MS[tf]
