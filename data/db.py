"""Persistence via SQLAlchemy (SQLite now, Postgres later without code changes).

Tables so far: oi_snapshots, incidents, symbol_meta (M1); signals, alerts_log, app_state (M2); config_versions (M3).
Trade tables (M4): see data/trade_db.py.
Nothing is ever deleted.
"""
from __future__ import annotations

import logging
import math
import time

from sqlalchemy import (JSON, BigInteger, Float, Index, Integer, String, Text, UniqueConstraint, create_engine,
                        func, select)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

log = logging.getLogger("db")


class Base(DeclarativeBase):
    pass


class OISnapshot(Base):
    __tablename__ = "oi_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32))
    ts: Mapped[int] = mapped_column(BigInteger)          # exchange engine time, ms UTC
    open_interest: Mapped[float] = mapped_column(Float)  # WEEX units (only % changes are used)
    __table_args__ = (Index("ix_oi_symbol_ts", "symbol", "ts"),)


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str] = mapped_column(Text)


class SymbolMeta(Base):
    __tablename__ = "symbol_meta"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    first_bar_ts: Mapped[int] = mapped_column(BigInteger)  # earliest daily kline = listing proxy
    updated_ts: Mapped[int] = mapped_column(BigInteger)


class SignalRow(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str | None] = mapped_column(String(16), unique=True, nullable=True)
    symbol: Mapped[str] = mapped_column(String(32))
    setup: Mapped[str] = mapped_column(String(16))
    bar_close_ms: Mapped[int] = mapped_column(BigInteger)
    created_ms: Mapped[int] = mapped_column(BigInteger)
    score: Mapped[int] = mapped_column(Integer)
    regime: Mapped[str] = mapped_column(String(16))
    session: Mapped[str] = mapped_column(String(16))
    config_hash: Mapped[str] = mapped_column(String(16))
    alert_status: Mapped[str] = mapped_column(String(16), default="pending")  # sent | suppressed | pending
    suppressed_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    data: Mapped[dict] = mapped_column(JSON)   # features, score breakdown, conditions, plan, levels, tags
    __table_args__ = (UniqueConstraint("symbol", "setup", "bar_close_ms", name="uq_signal_bar"),)


class AlertLog(Base):
    __tablename__ = "alerts_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(BigInteger)
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    signal_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    event: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))      # sent | console | failed | suppressed
    chat_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reply_to: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    text: Mapped[str] = mapped_column(Text)


class AppState(Base):
    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class ConfigVersion(Base):
    __tablename__ = "config_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(BigInteger)
    config_hash: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(32))        # startup | settings
    changes: Mapped[dict] = mapped_column(JSON)            # {"trade.margin_usd": [old, new], ...}
    config: Mapped[dict] = mapped_column(JSON)             # full config snapshot


def clean_json(x):
    """NaN/inf -> None so the JSON is valid everywhere (Postgres rejects NaN)."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): clean_json(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean_json(v) for v in x]
    if hasattr(x, "item") and not isinstance(x, (str, bytes)):  # numpy scalar
        return clean_json(x.item())
    return x


class Database:
    def __init__(self, url: str):
        self.engine = create_engine(url, future=True)
        import data.trade_db  # noqa: F401  (registers the trade tables)
        import highs.store  # noqa: F401  (registers the Highs tab tables)
        Base.metadata.create_all(self.engine)
        highs.store.migrate(self.engine)
        self.url = url
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> Session:
        return self.Session()

    # ---- incidents -------------------------------------------------------
    def incident(self, kind: str, message: str, symbol: str | None = None, severity: str = "warning") -> None:
        log.warning("INCIDENT %s %s %s", kind, symbol or "", message)
        with self.session() as s:
            s.add(Incident(ts=int(time.time() * 1000), kind=kind, severity=severity, symbol=symbol, message=message))
            s.commit()

    # ---- open interest ---------------------------------------------------
    def add_oi(self, rows: list[tuple[str, int, float]]) -> None:
        if not rows:
            return
        with self.session() as s:
            s.add_all(OISnapshot(symbol=sym, ts=ts, open_interest=oi) for sym, ts, oi in rows)
            s.commit()

    def load_oi_since(self, since_ms: int) -> list[tuple[str, int, float]]:
        with self.session() as s:
            q = select(OISnapshot.symbol, OISnapshot.ts, OISnapshot.open_interest).where(
                OISnapshot.ts >= since_ms).order_by(OISnapshot.ts)
            return [(a, b, c) for a, b, c in s.execute(q)]

    # ---- symbol meta -----------------------------------------------------
    def first_bar_ts(self, symbol: str) -> int | None:
        with self.session() as s:
            m = s.get(SymbolMeta, symbol)
            return m.first_bar_ts if m else None

    def set_first_bar_ts(self, symbol: str, ts: int) -> None:
        with self.session() as s:
            s.merge(SymbolMeta(symbol=symbol, first_bar_ts=ts, updated_ts=int(time.time() * 1000)))
            s.commit()

    # ---- signals ---------------------------------------------------------
    def insert_signal(self, *, symbol: str, setup: str, bar_close_ms: int, score: int, regime: str,
                      session: str, config_hash: str, data: dict) -> str | None:
        """Insert and assign signal_id (S-0001...). Returns None if this signal already exists
        (same symbol/setup/bar, e.g. after a restart) - never duplicated."""
        with self.session() as s:
            row = SignalRow(symbol=symbol, setup=setup, bar_close_ms=bar_close_ms, created_ms=int(time.time() * 1000),
                            score=score, regime=regime, session=session, config_hash=config_hash,
                            data=clean_json(data))
            s.add(row)
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                return None
            row.signal_id = f"S-{row.id:04d}"
            s.commit()
            return row.signal_id

    def update_signal_alert(self, signal_id: str, status: str, reason: str | None = None,
                            msg_id: int | None = None) -> None:
        with self.session() as s:
            row = s.execute(select(SignalRow).where(SignalRow.signal_id == signal_id)).scalar_one_or_none()
            if row:
                row.alert_status, row.suppressed_reason = status, reason
                if msg_id is not None:
                    row.telegram_msg_id = msg_id
                s.commit()

    def recent_signals(self, since_ms: int) -> list[SignalRow]:
        with self.session() as s:
            q = select(SignalRow).where(SignalRow.bar_close_ms >= since_ms).order_by(SignalRow.bar_close_ms)
            return list(s.execute(q).scalars())

    def get_signal(self, signal_id: str) -> SignalRow | None:
        with self.session() as s:
            return s.execute(select(SignalRow).where(SignalRow.signal_id == signal_id)).scalar_one_or_none()

    # ---- alerts log ------------------------------------------------------
    def alert_sent(self, dedupe_key: str) -> AlertLog | None:
        with self.session() as s:
            return s.execute(select(AlertLog).where(AlertLog.dedupe_key == dedupe_key)).scalar_one_or_none()

    def log_alert(self, *, dedupe_key: str, event: str, status: str, text: str, signal_id: str | None = None,
                  chat_id: str | None = None, message_id: int | None = None, reply_to: int | None = None) -> None:
        with self.session() as s:
            s.add(AlertLog(ts=int(time.time() * 1000), dedupe_key=dedupe_key, signal_id=signal_id, event=event,
                           status=status, chat_id=chat_id, message_id=message_id, reply_to=reply_to, text=text))
            try:
                s.commit()
            except IntegrityError:
                s.rollback()

    def count_alerts_since(self, event: str, since_ms: int) -> int:
        with self.session() as s:
            q = select(func.count()).select_from(AlertLog).where(
                AlertLog.event == event, AlertLog.ts >= since_ms, AlertLog.status.in_(("sent", "console")))
            return int(s.execute(q).scalar_one())

    # ---- config versions -------------------------------------------------
    def record_config(self, config_hash: str, config: dict, source: str, changes: dict | None = None) -> bool:
        """Store a config snapshot if it differs from the latest one. True if stored."""
        with self.session() as s:
            last = s.execute(select(ConfigVersion).order_by(ConfigVersion.id.desc()).limit(1)).scalar_one_or_none()
            if last is not None and last.config_hash == config_hash:
                return False
            s.add(ConfigVersion(ts=int(time.time() * 1000), config_hash=config_hash, source=source,
                                changes=clean_json(changes or {}), config=clean_json(config)))
            s.commit()
            return True

    def config_versions(self, limit: int = 10) -> list[ConfigVersion]:
        with self.session() as s:
            q = select(ConfigVersion).order_by(ConfigVersion.id.desc()).limit(limit)
            return list(s.execute(q).scalars())

    # ---- app state -------------------------------------------------------
    def get_state(self, key: str, default=None):
        with self.session() as s:
            row = s.get(AppState, key)
            return row.value.get("v", default) if row else default

    def set_state(self, key: str, value) -> None:
        with self.session() as s:
            s.merge(AppState(key=key, value={"v": value}))
            s.commit()
