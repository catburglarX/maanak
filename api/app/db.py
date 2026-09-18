"""Database engines and session helpers.

The API uses the asyncpg driver. Alembic and the worker's synchronous helpers use
psycopg. Both point at the same PostgreSQL instance.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import event, text
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

_async_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None
_sync_engine: Engine | None = None
_sync_session_factory: sessionmaker[Session] | None = None


def _connect_args() -> dict[str, object]:
    settings = get_settings()
    # asyncpg does not accept libpq-style options; the statement timeout is
    # applied per connection below instead.
    return {"server_settings": {"application_name": f"maanak-{settings.environment}"}}


def get_async_engine() -> AsyncEngine:
    global _async_engine
    if _async_engine is None:
        settings = get_settings()
        _async_engine = create_async_engine(
            settings.async_database_url,
            echo=settings.db_echo,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=1800,
            connect_args=_connect_args(),
        )
    return _async_engine


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            get_async_engine(),
            expire_on_commit=False,
            autoflush=False,
            class_=AsyncSession,
        )
    return _async_session_factory


def get_sync_engine() -> Engine:
    global _sync_engine
    if _sync_engine is None:
        settings = get_settings()
        _sync_engine = create_engine(
            settings.sync_database_url,
            echo=settings.db_echo,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_recycle=1800,
        )

        @event.listens_for(_sync_engine, "connect")
        def _set_statement_timeout(dbapi_connection, _record):  # pragma: no cover - driver hook
            with dbapi_connection.cursor() as cursor:
                cursor.execute(f"SET statement_timeout = {get_settings().db_statement_timeout_ms}")

    return _sync_engine


def get_sync_session_factory() -> sessionmaker[Session]:
    global _sync_session_factory
    if _sync_session_factory is None:
        _sync_session_factory = sessionmaker(
            get_sync_engine(), expire_on_commit=False, autoflush=False
        )
    return _sync_session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. Rolls back on any unhandled exception.

    After the handler returns, any background job created during the request is pushed
    to Redis. This happens here, and not where the job row is created, because a
    message must never reach a worker before the row it refers to is committed.
    """
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            await session.execute(
                text(f"SET LOCAL statement_timeout = {get_settings().db_statement_timeout_ms}")
            )
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            # Imported here: app.services.jobs reaches the models, which reach this
            # module, so a top-level import would be circular.
            from .services import jobs

            await jobs.push_pending(session)


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Async session with commit/rollback for background and startup work."""
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        else:
            from .services import jobs

            await jobs.push_pending(session)


@contextmanager
def sync_session_scope() -> Iterator[Session]:
    """Synchronous session used by the OCR worker."""
    factory = get_sync_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def dispose_engines() -> None:
    global _async_engine, _async_session_factory
    if _async_engine is not None:
        await _async_engine.dispose()
        _async_engine = None
        _async_session_factory = None


def reset_engines_for_tests() -> None:
    """Drop cached engines so a test can point at a different database."""
    global _async_engine, _async_session_factory, _sync_engine, _sync_session_factory
    _async_engine = None
    _async_session_factory = None
    if _sync_engine is not None:
        _sync_engine.dispose()
    _sync_engine = None
    _sync_session_factory = None
