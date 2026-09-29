"""Normalised market-data records and parsers for WEEX V3 payloads."""
from __future__ import annotations

import math
from typing import NamedTuple


class Bar(NamedTuple):
    t: int          # open time, ms UTC
    o: float
    h: float
    l: float
    c: float
    v: float        # base volume
    qv: float       # quote volume
    tbv: float      # taker-BUY base volume (after the WEEX field fix, see parse_kline)
    tbqv: float     # taker-BUY quote volume
    n: int          # trade count
    tc: int         # close time, ms UTC (== t + interval)


def parse_kline(row: list, taker_field_is_sell: bool) -> Bar:
    """Parse a REST kline row [open,o,h,l,c,vol,close,qv,n,V,Q].

    M0 finding: WEEX documents V/Q as taker-BUY volume, but live data shows they are
    taker-SELL volume (volume - V equals the trade tape's aggressive-buy volume exactly).
    With taker_field_is_sell=True we convert to taker-buy.
    """
    v, qv = float(row[5]), float(row[7])
    # Some older WEEX history has the taker fields empty (''): keep them UNKNOWN (NaN), never 0,
    # so order-flow conditions report "n/a" and fail instead of reading fake data.
    fv, fq = _num(row[9]), _num(row[10])
    tbv, tbqv = (v - fv, qv - fq) if taker_field_is_sell else (fv, fq)
    return Bar(
        t=int(row[0]), o=float(row[1]), h=float(row[2]), l=float(row[3]), c=float(row[4]),
        v=v, qv=qv, tbv=_floor0(tbv), tbqv=_floor0(tbqv), n=int(_num(row[8], 0)), tc=int(row[6]),
    )


def _num(x, default: float = math.nan) -> float:
    return default if x in (None, "") else float(x)


def _floor0(x: float) -> float:
    return x if math.isnan(x) else max(x, 0.0)


def parse_ws_kline(d: dict, taker_field_is_sell: bool) -> Bar:
    """Parse a WS kline item {t,T,o,c,h,l,v,n,q,V,Q}."""
    row = [d["t"], d["o"], d["h"], d["l"], d["c"], d["v"], d["T"], d["q"], d["n"], d["V"], d["Q"]]
    return parse_kline(row, taker_field_is_sell)


def normalize_klines(rows: list, now_ms: int, taker_field_is_sell: bool) -> list[Bar]:
    """WEEX returns bars newest-first and includes the forming bar.

    Returns closed bars only (close time <= now), de-duplicated, oldest-first.
    """
    by_t: dict[int, Bar] = {}
    for r in rows:
        b = parse_kline(r, taker_field_is_sell)
        if b.tc <= now_ms:
            by_t[b.t] = b
    return [by_t[t] for t in sorted(by_t)]


class Trade(NamedTuple):
    id: str
    t: int
    price: float
    qty: float
    quote: float
    taker_buy: bool   # True = aggressive buy


def parse_ws_trade(d: dict) -> Trade:
    # 'm' = buyer is maker -> taker was the seller. Verified live in M0 (trades vs book).
    return Trade(id=str(d["t"]), t=int(d["T"]), price=float(d["p"]), qty=float(d["q"]),
                 quote=float(d["v"]), taker_buy=not bool(d["m"]))
