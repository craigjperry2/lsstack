from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, cast

import pytest
from saq.queue.postgres import PostgresQueue
from sqlalchemy import select

from app.adapters.persistence import SqlAlchemyUnitOfWork
from app.adapters.persistence.models import OutboxMessageModel, TaskModel
from app.adapters.queue.relay import publish_message, relay_once
from app.adapters.queue.worker import WorkerResources, process_task_created_job
from app.application.auth import register
from app.application.outbox import claim_messages
from app.application.tasks import create_task
from tests.conftest import FakeClock
from tests.integration.test_postgres_contracts import FastPasswordHasher

if TYPE_CHECKING:
    from uuid import UUID

    from saq.types import Context
    from sqlalchemy.orm import Session

    from tests.integration.conftest import DatabaseHarness


class FailingQueue:
    async def enqueue(self, job_or_func: str, /, **kwargs: object) -> object | None:
        del job_or_func, kwargs
        raise RuntimeError("queue unavailable")


@pytest.mark.integration
async def test_real_postgres_saq_crash_retry_ack_and_job_idempotency(
    database_harness: DatabaseHarness,
) -> None:
    clock = FakeClock()

    def seed(session: Session) -> tuple[int, UUID]:
        uow = SqlAlchemyUnitOfWork(session)
        user = register(
            uow,
            FastPasswordHasher(),
            clock,
            email="queue-contract@example.com",
            password="correct-horse-battery",
        )
        created = create_task(
            uow,
            clock,
            user_id=user.user_id,
            title="Durable queue contract",
        )
        return created.task.id, created.outbox_message_id

    task_id, message_id = await database_harness.runner.run(seed)

    # A real application transaction records the failure and retains the lease,
    # so another relay cannot hot-loop the unavailable queue.
    assert (
        await relay_once(
            database_harness.runner,
            FailingQueue(),
            clock,
            owner="failed-relay",
            lease_duration=timedelta(seconds=1),
            batch_size=10,
        )
        == 0
    )

    def failed_state(session: Session) -> OutboxMessageModel:
        return session.get_one(OutboxMessageModel, message_id)

    failed = await database_harness.runner.run(failed_state)
    assert failed.published_at is None
    assert failed.lease_owner == "failed-relay"
    assert failed.attempt_count == 1
    assert failed.last_error == "RuntimeError: queue unavailable"

    queue = PostgresQueue.from_url(
        database_harness.settings.saq_database_url,
        min_size=1,
        max_size=2,
    )
    await queue.connect()
    try:
        assert (
            await relay_once(
                database_harness.runner,
                queue,
                clock,
                owner="early-relay",
                lease_duration=timedelta(seconds=1),
                batch_size=10,
            )
            == 0
        )

        # Model the crash window after a successful real SAQ enqueue but before
        # the application's acknowledgement transaction.
        clock.value += timedelta(seconds=2)
        claimed = await database_harness.runner.run(
            lambda session: claim_messages(
                SqlAlchemyUnitOfWork(session),
                clock,
                owner="crashed-relay",
                lease_duration=timedelta(seconds=1),
                limit=10,
            )
        )
        assert len(claimed) == 1
        await publish_message(queue, claimed[0])
        queued = await queue.job(str(message_id))
        assert queued is not None
        assert queued.function == "process_task_created_job"
        assert queued.kwargs == {"payload": {"version": 1, "task_id": task_id}}

        # Once the crashed lease expires, duplicate enqueue returns None and is
        # deliberately treated as success before the second transaction acks.
        clock.value += timedelta(seconds=2)
        assert (
            await relay_once(
                database_harness.runner,
                queue,
                clock,
                owner="recovery-relay",
                lease_duration=timedelta(seconds=1),
                batch_size=10,
            )
            == 1
        )
        published = await database_harness.runner.run(failed_state)
        assert published.published_at is not None
        assert published.lease_owner is None
        assert published.attempt_count == 3

        resources = WorkerResources(
            engine=database_harness.engine,
            runner=database_harness.runner,
            clock=clock,
            lease_duration=timedelta(seconds=1),
            batch_size=10,
        )
        context = cast("Context", {"app_resources": resources})
        payload = {"version": 1, "task_id": task_id}
        assert await process_task_created_job(context, payload=payload) == {
            "task_id": task_id,
            "processed": True,
        }

        def processed_at(session: Session) -> object:
            return session.scalar(
                select(TaskModel.background_processed_at).where(TaskModel.id == task_id)
            )

        first_processed_at = await database_harness.runner.run(processed_at)
        assert first_processed_at is not None
        clock.value += timedelta(seconds=1)
        assert await process_task_created_job(context, payload=payload) == {
            "task_id": task_id,
            "processed": True,
        }
        assert await database_harness.runner.run(processed_at) == first_processed_at
    finally:
        await queue.disconnect()
