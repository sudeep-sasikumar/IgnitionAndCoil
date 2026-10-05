"""Tables for the Highs tab: highs_coins (state per CoinGecko coin) and highs_events (every break).
Nothing is deleted; coins that leave the top N keep their row (and history) until they return."""
from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Boolean, Float, Index, Integer, String, inspect, or_, select, text
from sqlalchemy.orm import Mapped, mapped_column

from data.db import Base
from highs.detector import CoinState


class HighCoinRow(Base):
    __tablename__ = "highs_coins"
    cg_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(256))
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    ath: Mapped[float | None] = mapped_column(Float, nullable=True)
    ath_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    hist: Mapped[list] = mapped_column(JSON, default=list)
    hist_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    hist_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_ms: Mapped[int] = mapped_column(BigInteger, default=0)


class HighEventRow(Base):
    __tablename__ = "highs_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cg_id: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(256))
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(8))                  # ATH | 52W
    ts: Mapped[int] = mapped_column(BigInteger)
    price: Mapped[float] = mapped_column(Float)                   # price at detection
    level: Mapped[float] = mapped_column(Float)                   # the new high
    prev_high: Mapped[float] = mapped_column(Float)
    prev_high_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    weex_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    alert_status: Mapped[str | None] = mapped_column(String(16), nullable=True)   # sent | console | skipped
    peak: Mapped[float | None] = mapped_column(Float, nullable=True)              # highest price since the break
    peak_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)        # when (None = not filled yet)
    __table_args__ = (Index("ix_highs_events_ts", "ts"),)


def migrate(engine) -> None:
    """Add the columns newer versions introduced to an existing highs_events table."""
    have = {c["name"] for c in inspect(engine).get_columns("highs_events")}
    with engine.begin() as con:
        for name, kind in (("peak", "FLOAT"), ("peak_ms", "BIGINT")):
            if name not in have:
                con.execute(text(f"ALTER TABLE highs_events ADD COLUMN {name} {kind}"))


def load_states(db) -> dict[str, tuple[CoinState, HighCoinRow]]:
    with db.session() as s:
        rows = s.scalars(select(HighCoinRow)).all()
    return {r.cg_id: (CoinState(r.cg_id, r.symbol, r.name, r.ath, r.ath_ms, [list(x) for x in (r.hist or [])],
                                bool(r.hist_ok), int(r.hist_ms or 0)), r) for r in rows}


def save_coins(db, rows: list[dict]) -> None:
    """Upsert coin rows (dicts with HighCoinRow columns)."""
    if not rows:
        return
    with db.session() as s:
        for d in rows:
            s.merge(HighCoinRow(**d))
        s.commit()


def add_events(db, events: list[dict]) -> list[int]:
    with db.session() as s:
        objs = [HighEventRow(**e) for e in events]
        s.add_all(objs)
        s.commit()
        return [o.id for o in objs]


def set_alert_status(db, ids: list[int], status: str) -> None:
    if not ids:
        return
    with db.session() as s:
        for o in s.scalars(select(HighEventRow).where(HighEventRow.id.in_(ids))):
            o.alert_status = status
        s.commit()


def peak_events(db, since_ms: int) -> list[HighEventRow]:
    """Events whose peak is still tracked (newer than since_ms) or was never filled (any age)."""
    with db.session() as s:
        return list(s.scalars(select(HighEventRow).where(or_(HighEventRow.ts >= since_ms, HighEventRow.peak.is_(None)))))


def set_peaks(db, peaks: dict[int, tuple[float, int]]) -> None:
    if not peaks:
        return
    with db.session() as s:
        for o in s.scalars(select(HighEventRow).where(HighEventRow.id.in_(list(peaks)))):
            o.peak, o.peak_ms = peaks[o.id]
        s.commit()


def events_since(db, since_ms: int) -> list[HighEventRow]:
    with db.session() as s:
        return list(s.scalars(select(HighEventRow).where(HighEventRow.ts >= since_ms).order_by(HighEventRow.ts.desc())))
