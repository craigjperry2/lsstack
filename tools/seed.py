"""Idempotent local development data seed."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import anyio

from app.adapters.persistence import (
    SqlAlchemyUnitOfWork,
    TransactionRunner,
    create_async_session_factory,
)
from app.adapters.persistence.engine import create_engine
from app.adapters.security.clock import SystemClock
from app.adapters.security.passwords import PwdlibPasswordHasher
from app.application.auth import register
from app.application.tasks import create_task, toggle_task
from app.config import Settings, load_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

SEED_TASKS = (
    ("Review the agent workflow", "Explore the Justfile and AGENTS.md.", False),
    (
        "Inspect observability traces",
        "Follow a request through the local stack.",
        False,
    ),
    ("Run the fast test suite", "Use `just test-fast` before committing.", True),
)


@dataclass(frozen=True, slots=True)
class SeedResult:
    email: str
    user_created: bool
    password_repaired: bool
    tasks_created: int


def validate_seed_target(settings: Settings) -> None:
    """Refuse anything except an explicitly local loopback database."""
    if settings.environment not in {"local", "development"}:
        raise RuntimeError(
            "Database seeding is allowed only when APP_ENV is local or development."
        )
    hostname = urlparse(settings.database_url).hostname
    if hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(
            "Database seeding requires DATABASE_URL to use localhost or 127.0.0.1."
        )


def seed_operation(
    session: Session,
    *,
    email: str,
    password: str,
    passwords: PwdlibPasswordHasher,
) -> SeedResult:
    """Create or repair the demo identity and create missing sample tasks."""
    uow = SqlAlchemyUnitOfWork(session)
    clock = SystemClock()
    user = uow.users.get_by_email_for_update(email.casefold())
    user_created = user is None
    password_repaired = False
    if user is None:
        auth = register(
            uow,
            passwords,
            clock,
            email=email,
            password=password,
        )
        user_id: int = auth.user_id
    else:
        if user.id is None:
            raise RuntimeError("Seed user unexpectedly has no persisted ID.")
        verification = passwords.verify(password, user.password_hash)
        if not verification.valid:
            user = uow.users.update_credentials(
                user.with_password(passwords.hash(password), clock.now())
            )
            password_repaired = True
        elif verification.replacement_hash is not None:
            user = uow.users.update_credentials(
                user.with_rehashed_password(
                    verification.replacement_hash,
                    clock.now(),
                )
            )
        if user.id is None:
            raise RuntimeError("Seed user unexpectedly lost its persisted ID.")
        user_id = user.id

    existing_titles = {task.title for task in uow.tasks.list_owned(user_id)}
    tasks_created = 0
    for title, description, completed in SEED_TASKS:
        if title in existing_titles:
            continue
        created = create_task(
            uow,
            clock,
            user_id=user_id,
            title=title,
            description=description,
        )
        if completed:
            toggle_task(
                uow,
                clock,
                user_id=user_id,
                task_id=created.task.id,
            )
        tasks_created += 1

    return SeedResult(
        email=email.casefold(),
        user_created=user_created,
        password_repaired=password_repaired,
        tasks_created=tasks_created,
    )


async def seed() -> SeedResult:
    settings = load_settings(env_file=None)
    validate_seed_target(settings)
    email = os.environ.get("DEV_SEED_EMAIL", "agent@example.test")
    password = os.environ.get("DEV_SEED_PASSWORD", "correct-horse-battery")
    engine = create_engine(settings.database_url)
    runner = TransactionRunner(create_async_session_factory(engine))
    passwords = PwdlibPasswordHasher()
    try:
        return await runner.run(
            lambda session: seed_operation(
                session,
                email=email,
                password=password,
                passwords=passwords,
            )
        )
    finally:
        await engine.dispose()


def main() -> None:
    result = anyio.run(seed)
    print(f"Seed account: {result.email}")
    print(
        "User: "
        + (
            "created"
            if result.user_created
            else "password repaired"
            if result.password_repaired
            else "already current"
        )
    )
    print(f"Tasks created: {result.tasks_created}")


if __name__ == "__main__":
    main()
