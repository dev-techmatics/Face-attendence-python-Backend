"""Async engine and session factory.

One engine per process; a session per request. The session doubles as the unit of work, which
is what the services commit through.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the engine for the configured database URL."""
    url = settings.database.url
    kwargs: dict[str, object] = {"echo": settings.database.echo, "future": True}

    if url.startswith("sqlite"):
        # SQLite has no server to pool against, and a file-backed database is only reachable
        # from this process anyway.
        kwargs["connect_args"] = {"timeout": settings.database.command_timeout_seconds}
    else:
        # Recycle before a typical idle-connection timeout, and verify a pooled connection
        # before handing it out: a kiosk deployment sits idle for hours between shifts.
        kwargs.update(
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=10,
            max_overflow=10,
        )

    return create_async_engine(url, **kwargs)


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _session_factory

    if _engine is None:
        _engine = create_engine(settings)
        _session_factory = async_sessionmaker(
            _engine, expire_on_commit=False, class_=AsyncSession
        )

    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("The database engine has not been initialised yet.")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("The database engine has not been initialised yet.")
    return _session_factory


async def dispose_engine() -> None:
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()

    _engine = None
    _session_factory = None


async def session_scope() -> AsyncIterator[AsyncSession]:
    """A session for work outside a request, such as the retention job."""
    factory = get_session_factory()
    async with factory() as session:
        yield session
