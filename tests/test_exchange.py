"""Kline normalisation (WEEX quirks from M0), trade parsing, rate limiter, clock helpers."""
import asyncio
import time

from core.clock import last_closed_open
from exchange.models import normalize_klines, parse_kline, parse_ws_trade
from exchange.ratelimit import WeightLimiter

M5 = 300_000


def row(t, c, v=10.0, qv=1000.0, V=4.0, Q=400.0, step=M5):
    return [t, str(c), str(c + 1), str(c - 1), str(c), str(v), t + step, str(qv), 5, str(V), str(Q)]


def test_taker_field_is_sell_conversion():
    b = parse_kline(row(0, 100), taker_field_is_sell=True)
    assert b.tbv == 6.0 and b.tbqv == 600.0          # buy = total - "taker" field
    b2 = parse_kline(row(0, 100), taker_field_is_sell=False)
    assert b2.tbv == 4.0 and b2.tbqv == 400.0


def test_missing_taker_fields_are_unknown_not_zero():
    import math
    r = row(0, 100)
    r[9] = r[10] = ""                      # seen in old WEEX history
    b = parse_kline(r, taker_field_is_sell=True)
    assert math.isnan(b.tbv) and math.isnan(b.tbqv) and b.v == 10.0


def test_normalize_newest_first_dedupe_and_drop_forming():
    now = 3 * M5 + 10                                  # bar 3 is still forming
    rows = [row(3 * M5, 104), row(2 * M5, 103), row(1 * M5, 102), row(2 * M5, 103.5), row(0, 101)]
    bars = normalize_klines(rows, now, True)
    assert [b.t for b in bars] == [0, M5, 2 * M5]      # ascending, forming bar dropped
    assert bars[-1].c == 103.5                         # later duplicate wins


def test_ws_trade_side():
    assert parse_ws_trade({"t": "a", "T": 1, "p": "1", "q": "2", "v": "2", "m": False}).taker_buy is True
    assert parse_ws_trade({"t": "b", "T": 1, "p": "1", "q": "2", "v": "2", "m": True}).taker_buy is False


def test_next_bar_close_catches_up():
    from core.clock import next_bar_close
    # normal: processed 10:00 -> next is 10:05
    assert next_bar_close(12 * M5, 12 * M5 + 60_000) == 13 * M5
    # startup finished just after a boundary (bar 13 closed but not processed): process it now
    assert next_bar_close(12 * M5, 13 * M5 + 1_000) == 13 * M5
    # woke from sleep 1h later: jump to the newest closed bar, not the next missed one
    assert next_bar_close(12 * M5, 24 * M5 + 30_000) == 24 * M5
    assert next_bar_close(0, 12 * M5 + 5) == 13 * M5


def test_last_closed_open():
    assert last_closed_open(10 * M5 + 1, "5m") == 9 * M5
    assert last_closed_open(10 * M5, "5m") == 9 * M5


async def test_weight_limiter_blocks_until_window_expires():
    lim = WeightLimiter(max_weight=3, window_s=0.3)
    t0 = time.monotonic()
    for _ in range(3):
        await lim.acquire(1)
    assert time.monotonic() - t0 < 0.1
    await lim.acquire(1)                               # must wait for the window
    assert time.monotonic() - t0 >= 0.28
    assert lim.used() <= 3


async def test_weight_limiter_concurrent_never_exceeds():
    lim = WeightLimiter(max_weight=5, window_s=0.2)
    await asyncio.gather(*(lim.acquire(1) for _ in range(12)))
    assert lim.used() <= 5


async def test_weight_limiter_syncs_with_server_counter():
    t = [0.0]
    lim = WeightLimiter(max_weight=10, window_s=10, clock=lambda: t[0])
    await lim.acquire(2)
    lim.sync(9)                       # server saw 9: 7 came from another process on our IP
    assert lim.used() == 9
    lim.sync(5)                       # lower server number never reduces our own accounting
    assert lim.used() == 9
    t[0] = 10.5                       # window passed: everything expires
    assert lim.used() == 0
