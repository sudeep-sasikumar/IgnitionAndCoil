"""P&L for your logged trades - from what you logged, fees and funding included (spec §8.2).

Your fills are real prices, so no slippage allowance is added. Fee per leg follows the order
type you logged (market = taker, limit = maker).
"""
from __future__ import annotations


def fee_rate(cfg, order: str) -> float:
    return cfg.trade.maker_fee if order == "limit" else cfg.trade.taker_fee


def confirmed_pnl(cfg, entry: float, qty: float, entry_order: str, legs: list[dict], funding: float = 0.0) -> float:
    """Net $ from logged exit legs [{price, qty, order}] plus the entry fee on the closed part.
    For a fully closed trade this is the final P&L."""
    closed = sum(float(l["qty"]) for l in legs)
    net = -closed * entry * fee_rate(cfg, entry_order)
    for l in legs:
        q, px = float(l["qty"]), float(l["price"])
        net += q * (px - entry) - q * px * fee_rate(cfg, l.get("order", "market"))
    return net + funding


def mark_to_market(cfg, entry: float, qty: float, entry_order: str, legs: list[dict], price: float,
                   funding: float = 0.0) -> float:
    """Unconfirmed P&L: logged legs + the open remainder valued at `price` (market exit fee)."""
    closed = sum(float(l["qty"]) for l in legs)
    open_q = max(qty - closed, 0.0)
    net = confirmed_pnl(cfg, entry, qty, entry_order, legs, funding)
    net += -open_q * entry * fee_rate(cfg, entry_order)
    net += open_q * (price - entry) - open_q * price * cfg.trade.taker_fee
    return net


def risk_usd(cfg, entry: float, qty: float, entry_order: str, stop: float) -> float:
    """$ lost if the whole position is stopped at `stop` (market exit)."""
    return abs(qty * (stop - entry) - qty * entry * fee_rate(cfg, entry_order) - qty * stop * cfg.trade.taker_fee)
