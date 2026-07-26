from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from app.adapters.persistence.models import TaskModel, UserModel
from app.adapters.security.passwords import PwdlibPasswordHasher
from tools.seed import SEED_TASKS, seed_operation

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from tests.integration.conftest import DatabaseHarness


@pytest.mark.integration
async def test_seed_is_idempotent_and_repairs_the_demo_password(
    database_harness: DatabaseHarness,
) -> None:
    passwords = PwdlibPasswordHasher()
    email = "agent@example.test"
    password = "correct-horse-battery"

    first = await database_harness.runner.run(
        lambda session: seed_operation(
            session,
            email=email,
            password=password,
            passwords=passwords,
        )
    )
    second = await database_harness.runner.run(
        lambda session: seed_operation(
            session,
            email=email,
            password=password,
            passwords=passwords,
        )
    )

    def counts(session: Session) -> tuple[int, int, str]:
        user = session.scalar(
            select(UserModel).where(UserModel.email_normalized == email)
        )
        assert user is not None
        user_count = session.scalar(select(func.count()).select_from(UserModel))
        task_count = session.scalar(
            select(func.count())
            .select_from(TaskModel)
            .where(TaskModel.user_id == user.id)
        )
        assert user_count is not None
        assert task_count is not None
        return user_count, task_count, user.password_hash

    user_count, task_count, password_hash = await database_harness.runner.run(counts)
    assert first.user_created is True
    assert first.tasks_created == len(SEED_TASKS)
    assert second.user_created is False
    assert second.password_repaired is False
    assert second.tasks_created == 0
    assert user_count == 1
    assert task_count == len(SEED_TASKS)
    assert passwords.verify(password, password_hash).valid is True
