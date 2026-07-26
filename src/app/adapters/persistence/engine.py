"""Async SQLAlchemy lifecycle and cancellation-safe synchronous transaction bridge."""

import asyncio
from collections.abc import Callable
from typing import TypeVar

import anyio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

T = TypeVar("T")
SyncOperation = Callable[[Session], T]


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
    )


def create_async_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


class TransactionRunner:
    """Run exactly one synchronous application callable per fresh async session."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cleanup_timeout_seconds: float = 5.0,
    ) -> None:
        if cleanup_timeout_seconds <= 0:
            raise ValueError("Transaction cleanup timeout must be positive.")
        self._session_factory = session_factory
        self._cleanup_timeout_seconds = cleanup_timeout_seconds

    async def _rollback(self, session: AsyncSession) -> None:
        with anyio.move_on_after(self._cleanup_timeout_seconds, shield=True):
            await session.rollback()

    async def _commit(self, session: AsyncSession) -> None:
        with anyio.fail_after(self._cleanup_timeout_seconds, shield=True):
            await session.commit()

    async def run(self, operation: SyncOperation[T]) -> T:
        """Execute, commit, and return a detached result.

        Only SQLAlchemy-adapted I/O is permitted inside ``operation``.
        """
        async with self._session_factory() as session:
            try:
                result = await session.run_sync(operation)
                await self._commit(session)
            except asyncio.CancelledError:
                await self._rollback(session)
                raise
            except BaseException:
                await self._rollback(session)
                raise
            else:
                return result

    async def probe(self) -> bool:
        """Readiness-friendly database round trip through the normal bridge."""

        def operation(session: Session) -> bool:
            return session.scalar(text("SELECT true")) is True

        return await self.run(operation)
