from __future__ import annotations

import os
from typing import AsyncIterator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _db_disabled() -> bool:
    flag = os.getenv("DISABLE_DB", "").strip().lower()
    return flag in {"1", "true", "yes", "y", "on"}


def get_database_url() -> str:
    if _db_disabled():
        raise RuntimeError("DATABASE_URL is disabled (DISABLE_DB=1)")
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_database_url(), pool_pre_ping=True)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def init_db() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _run_migrations(conn)


async def _run_migrations(conn) -> None:
    try:
        dialect = conn.engine.dialect.name
    except Exception:
        dialect = ""

    if dialect == "sqlite":
        # Skip PostgreSQL-specific migrations when running locally on SQLite
        return

    await conn.execute(
        text(
            """
            ALTER TABLE payments
            ADD COLUMN IF NOT EXISTS client_uuid VARCHAR(64);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_payments_client_uuid
            ON payments (client_uuid);
            """
        )
    )


async def get_db() -> AsyncIterator[Optional[AsyncSession]]:
    if _db_disabled():
        yield None
        return
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        await session.close()
