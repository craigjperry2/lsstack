from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from app.adapters.persistence import SqlAlchemyUnitOfWork
from app.adapters.persistence.models import OutboxMessageModel, TaskModel, UserModel
from app.adapters.security.clock import SystemClock
from app.application.auth import register
from app.application.ports.security import PasswordVerification
from app.application.tasks import create_task

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from tests.integration.conftest import DatabaseHarness


class FastPasswordHasher:
    def hash(self, password: str) -> str:
        return f"test:{password}"

    def verify(self, password: str, password_hash: str) -> PasswordVerification:
        return PasswordVerification(password_hash == self.hash(password))

    def verify_unknown(self, password: str) -> None:
        return None


@pytest.mark.integration
async def test_migration_empty_upgrade_and_downgrade_roundtrip(
    database_harness: DatabaseHarness,
) -> None:
    command.downgrade(database_harness.alembic, "base")
    command.upgrade(database_harness.alembic, "head")

    def table_names(session: Session) -> set[str]:
        rows = session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'app'"
            )
        )
        return {str(row[0]) for row in rows}

    assert await database_harness.runner.run(table_names) >= {
        "users",
        "tasks",
        "outbox_messages",
        "alembic_version",
    }


@pytest.mark.integration
async def test_roles_cannot_access_each_others_schema(
    database_harness: DatabaseHarness,
) -> None:
    app_engine = create_async_engine(database_harness.settings.database_url)
    saq_engine = create_async_engine(
        make_url(database_harness.settings.saq_database_url).set(
            drivername="postgresql+psycopg"
        )
    )
    try:
        async with app_engine.connect() as connection:
            with pytest.raises(ProgrammingError, match="permission denied"):
                await connection.execute(text("CREATE TABLE saq.forbidden(id int)"))
        async with saq_engine.connect() as connection:
            saq_tables = {
                str(row[0])
                for row in await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'saq'"
                    )
                )
            }
            assert saq_tables >= {"saq_jobs", "saq_stats", "saq_versions"}
            with pytest.raises(ProgrammingError, match="permission denied"):
                await connection.execute(text("SELECT * FROM app.users"))
    finally:
        await app_engine.dispose()
        await saq_engine.dispose()


@pytest.mark.integration
async def test_task_and_outbox_are_atomic_and_rollback_together(
    database_harness: DatabaseHarness,
) -> None:
    clock = SystemClock()
    passwords = FastPasswordHasher()

    def create_user(session: Session) -> int:
        return register(
            SqlAlchemyUnitOfWork(session),
            passwords,
            clock,
            email="atomic@example.com",
            password="correct-horse-battery",
        ).user_id

    user_id = await database_harness.runner.run(create_user)

    def create_success(session: Session) -> int:
        result = create_task(
            SqlAlchemyUnitOfWork(session),
            clock,
            user_id=user_id,
            title="Committed task",
        )
        return result.task.id

    await database_harness.runner.run(create_success)

    class RollbackMarkerError(Exception):
        pass

    def create_failure(session: Session) -> None:
        create_task(
            SqlAlchemyUnitOfWork(session),
            clock,
            user_id=user_id,
            title="Rolled-back task",
        )
        raise RollbackMarkerError

    with pytest.raises(RollbackMarkerError):
        await database_harness.runner.run(create_failure)

    def counts(session: Session) -> tuple[int, int, int]:
        return (
            session.scalar(select(func.count()).select_from(UserModel)) or 0,
            session.scalar(select(func.count()).select_from(TaskModel)) or 0,
            session.scalar(select(func.count()).select_from(OutboxMessageModel)) or 0,
        )

    assert await database_harness.runner.run(counts) == (1, 1, 1)
