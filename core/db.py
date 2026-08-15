from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


# ---------------------------------------------------------------------------
# Connection strings
# ---------------------------------------------------------------------------
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://quantdesk:quantdesk@quantdesk-postgres:5432/quantdesk",
)

SYNC_DATABASE_URL: str = os.getenv(
    "SYNC_DATABASE_URL",
    "postgresql://quantdesk:quantdesk@quantdesk-postgres:5432/quantdesk",
)

# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------
async_engine = create_async_engine(DATABASE_URL, echo=False, future=True)
sync_engine = create_engine(SYNC_DATABASE_URL, echo=False, future=True)

# ---------------------------------------------------------------------------
# Session factories
# ---------------------------------------------------------------------------
AsyncSessionLocal = sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

SyncSessionLocal = sessionmaker(bind=sync_engine, class_=Session, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Declarative base
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Strategy(Base):
    __tablename__ = "strategies"

    id: str = Column(String, primary_key=True, default=_uuid)
    name: str = Column(String, nullable=False)
    type: str = Column(String, nullable=False, default="custom")
    description: str = Column(Text, nullable=True)
    enabled: bool = Column(Boolean, default=True, nullable=False)
    params: dict = Column(JSON, nullable=True)
    code: str = Column(Text, nullable=True)
    metrics: dict = Column(JSON, nullable=True)
    created_at: datetime = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: datetime = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class Settings(Base):
    __tablename__ = "settings"

    key: str = Column(String, primary_key=True)
    value: str = Column(Text, nullable=True)
    updated_at: datetime = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class Order(Base):
    __tablename__ = "orders"

    id: str = Column(String, primary_key=True)
    symbol: str = Column(String, nullable=False, index=True)
    qty: float = Column(Float, nullable=False)
    filled_qty: float = Column(Float, default=0.0, nullable=False)
    side: str = Column(String, nullable=False)          # buy / sell
    type: str = Column(String, nullable=False)           # market / limit / stop
    status: str = Column(String, nullable=False, default="new")
    limit_price: float = Column(Float, nullable=True)
    stop_price: float = Column(Float, nullable=True)
    filled_avg_price: float = Column(Float, nullable=True)
    strategy_id: str = Column(String, ForeignKey("strategies.id"), nullable=True)
    submitted_at: datetime = Column(DateTime(timezone=True), nullable=True)
    filled_at: datetime = Column(DateTime(timezone=True), nullable=True)
    canceled_at: datetime = Column(DateTime(timezone=True), nullable=True)
    created_at: datetime = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class Transaction(Base):
    __tablename__ = "transactions"

    id: str = Column(String, primary_key=True, default=_uuid)
    order_id: str = Column(String, ForeignKey("orders.id"), nullable=False, index=True)
    symbol: str = Column(String, nullable=False)
    side: str = Column(String, nullable=False)
    qty: float = Column(Float, nullable=False)
    price: float = Column(Float, nullable=False)
    fee: float = Column(Float, default=0.0, nullable=False)
    strategy_id: str = Column(String, ForeignKey("strategies.id"), nullable=True)
    created_at: datetime = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class BacktestResult(Base):
    __tablename__ = "backtest_results"

    id: str = Column(String, primary_key=True, default=_uuid)
    strategy_id: str = Column(String, ForeignKey("strategies.id"), nullable=False, index=True)
    symbol: str = Column(String, nullable=False)
    params: dict = Column(JSON, nullable=True)
    result: dict = Column(JSON, nullable=True)
    created_at: datetime = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    user: str = Column(String, nullable=False)
    action: str = Column(String, nullable=False)
    detail: str = Column(Text, nullable=True)
    ip: str = Column(String, nullable=True)
    created_at: datetime = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

async def create_all() -> None:
    """Create all tables (async)."""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def create_all_sync() -> None:
    """Create all tables (sync)."""
    Base.metadata.create_all(bind=sync_engine)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI async dependency that yields a database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_sync_session() -> Session:
    """Return a synchronous SQLAlchemy session."""
    return SyncSessionLocal()
