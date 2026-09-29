"""Trade tables (spec §8.4): paper_trades, manual_trades, manual_trade_events, trade_events.

Nothing is ever deleted: manual_trade_events is append-only (edits store old -> new), and
closed trades stay forever.
"""
from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Boolean, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from data.db import Base


class PaperTradeRow(Base):
    __tablename__ = "paper_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(16))
    policy: Mapped[str] = mapped_column(String(2))                 # L | S
    symbol: Mapped[str] = mapped_column(String(32))
    setup: Mapped[str] = mapped_column(String(16))
    score: Mapped[int] = mapped_column(Integer)
    regime: Mapped[str] = mapped_column(String(16))
    session: Mapped[str] = mapped_column(String(16))
    tags: Mapped[list] = mapped_column(JSON)
    suppressed_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(12))                # OPEN | CLOSED
    entry_ms: Mapped[int] = mapped_column(BigInteger)
    entry_ref: Mapped[float] = mapped_column(Float)
    entry_fill: Mapped[float] = mapped_column(Float)
    qty: Mapped[float] = mapped_column(Float)
    notional: Mapped[float] = mapped_column(Float)
    margin: Mapped[float] = mapped_column(Float)
    leverage: Mapped[float] = mapped_column(Float)
    initial_stop: Mapped[float] = mapped_column(Float)
    tp1: Mapped[float] = mapped_column(Float)
    tp2: Mapped[float | None] = mapped_column(Float, nullable=True)
    liq_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_usd: Mapped[float] = mapped_column(Float)                 # $ lost at the initial stop (fees incl.)
    exit_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    net_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp1_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tp2_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    legs: Mapped[list] = mapped_column(JSON)                        # [{ts, price, fraction, kind, reason}]
    state: Mapped[dict] = mapped_column(JSON)                       # ExitState
    config_hash: Mapped[str] = mapped_column(String(16))
    updated_ms: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (UniqueConstraint("signal_id", "policy", name="uq_paper_signal_policy"),
                      Index("ix_paper_status", "status"))


class ManualTradeRow(Base):
    __tablename__ = "manual_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_ref: Mapped[str | None] = mapped_column(String(16), unique=True, nullable=True)   # M-0001
    signal_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str] = mapped_column(String(16))                # signal | manual_own
    symbol: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24))                # OPEN | NEEDS_CONFIRMATION | CLOSED
    entry_ms: Mapped[int] = mapped_column(BigInteger)
    entry_price: Mapped[float] = mapped_column(Float)
    entry_order: Mapped[str] = mapped_column(String(8))            # market | limit
    margin: Mapped[float] = mapped_column(Float)
    leverage: Mapped[float] = mapped_column(Float)
    notional: Mapped[float] = mapped_column(Float)
    qty: Mapped[float] = mapped_column(Float)
    qty_open: Mapped[float] = mapped_column(Float)
    stop_policy: Mapped[str] = mapped_column(String(8))            # L | S | custom
    initial_stop: Mapped[float] = mapped_column(Float)
    current_stop: Mapped[float] = mapped_column(Float)             # your LOGGED stop
    tp1: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp2: Mapped[float | None] = mapped_column(Float, nullable=True)
    liq_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_usd: Mapped[float] = mapped_column(Float)
    notes: Mapped[str] = mapped_column(Text, default="")
    warnings: Mapped[list] = mapped_column(JSON)
    legs: Mapped[list] = mapped_column(JSON)                        # logged exits [{ts, price, qty, order, kind}]
    net_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)       # confirmed, when closed
    funding_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    plan_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)      # exit engine on your entry
    entry_gap_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    shadow: Mapped[dict] = mapped_column(JSON)                      # ExitState run on your entry/stop
    needs_confirmation: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_trail_alert_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    thread_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    replayed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_ms: Mapped[int] = mapped_column(BigInteger)
    closed_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_ms: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (Index("ix_manual_status", "status"),)


class ManualTradeEventRow(Base):
    """Append-only audit trail of everything you logged or the system flagged."""
    __tablename__ = "manual_trade_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[int] = mapped_column(BigInteger)                    # when it happened (as logged)
    logged_ms: Mapped[int] = mapped_column(BigInteger)             # when it was recorded
    kind: Mapped[str] = mapped_column(String(24))   # OPEN PARTIAL_CLOSE CLOSE STOP_MOVE EDIT NEEDS_CONFIRMATION ...
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    old: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")


class TradeEventRow(Base):
    """Exit-engine events for paper and manual (plan shadow) trades."""
    __tablename__ = "trade_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_type: Mapped[str] = mapped_column(String(8))             # paper | manual
    trade_id: Mapped[int] = mapped_column(Integer)
    ts: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))
    price: Mapped[float] = mapped_column(Float)
    fraction: Mapped[float] = mapped_column(Float)
    stop_before: Mapped[float] = mapped_column(Float)
    stop_after: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("trade_type", "trade_id", "kind", "ts", "stop_after", name="uq_trade_event"),
                      Index("ix_trade_events_trade", "trade_type", "trade_id"))
