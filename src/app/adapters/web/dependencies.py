"""Typed composition seam between Litestar handlers and synchronous use cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session

    from app.application.ports.clock import Clock
    from app.application.ports.persistence import UnitOfWork
    from app.application.ports.security import PasswordHasher, PublicIdCodec

T = TypeVar("T")


class TransactionRunner(Protocol):
    async def run(self, operation: Callable[[Session], T]) -> T:
        """Run one synchronous operation in a request-scoped transaction."""
        ...

    async def probe(self) -> bool:
        """Return whether the database accepts a request-scoped round trip."""
        ...


@dataclass(frozen=True, slots=True)
class WebDependencies:
    transactions: TransactionRunner
    unit_of_work: Callable[[Session], UnitOfWork]
    passwords: PasswordHasher
    clock: Clock
    public_ids: PublicIdCodec
    session_lifetime_seconds: int
