"""P&L with fees and slippage (spec §2). Shared by plans, paper trades and the journal."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Leg:
    price: float
    fraction: float     # of the original position
    kind: str           # "stop" | "market" (taker + slippage) | "tp" (limit, maker, no slippage)


def fee_rate(cfg, which: str) -> float:
    t = cfg.trade
    return t.taker_fee if which == "taker" else t.maker_fee


def _sign(side: str) -> int:
    return -1 if side == "SHORT" else 1


def entry_fill(ref: float, cfg, market: bool = True, side: str = "LONG") -> float:
    """Slippage always works against you: a long buys higher, a short sells lower."""
    return ref * (1 + _sign(side) * cfg.trade.slippage_pct / 100) if market else ref


def exit_fill(price: float, kind: str, cfg, side: str = "LONG") -> float:
    return price * (1 - _sign(side) * cfg.trade.slippage_pct / 100) if kind in ("stop", "market") else price


def exit_fee_rate(kind: str, cfg) -> float:
    p = cfg.plan
    return fee_rate(cfg, p.tp_fee if kind == "tp" else p.stop_fee)


def pnl_usd(entry_price: float, qty: float, legs: list[Leg], cfg, funding_usd: float = 0.0,
            entry_is_fill: bool = False, side: str = "LONG") -> float:
    """Net $ P&L (long by default). entry_price is a reference price (slippage added) unless
    entry_is_fill. funding_usd is signed (+ received, - paid)."""
    fill = entry_price if entry_is_fill else entry_fill(entry_price, cfg, side=side)
    net = -qty * fill * fee_rate(cfg, cfg.plan.entry_fee)
    s = _sign(side)
    for leg in legs:
        q = qty * leg.fraction
        px = exit_fill(leg.price, leg.kind, cfg, side=side)
        net += s * q * (px - fill) - q * px * exit_fee_rate(leg.kind, cfg)
    return net + funding_usd


def breakeven_winrate(loss_usd: float, win_usd: float) -> float:
    """Win rate at which average P&L is zero, given a loss (negative) and a win (positive)."""
    loss = abs(loss_usd)
    if win_usd <= 0:
        return 1.0
    return loss / (loss + win_usd)
