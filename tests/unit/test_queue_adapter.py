from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, TypeVar, cast
from uuid import UUID, uuid4

import pytest
from litestar import Litestar
from litestar_saq import SAQPlugin

from app.adapters.queue import relay as relay_module
from app.adapters.queue import worker as worker_module
from app.adapters.queue.relay import relay_once
from app.adapters.queue.worker import (
    WorkerResources,
    build_saq_plugin,
    process_task_created_job,
)
from app.application.outbox import ClaimedMessage
from app.config import Settings
from tests.conftest import FakeClock

if TYPE_CHECKING:
    from collections.abc import Callable

    from saq.types import Context
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.orm import Session

    from app.adapters.persistence import TransactionRunner
    from app.application.ports.clock import Clock
    from app.application.ports.persistence import UnitOfWork

T = TypeVar("T")


def test_saq_plugin_keeps_exact_cli_type_without_web_heartbeat() -> None:
    plugin = build_saq_plugin(Settings())
    assert type(plugin) is SAQPlugin
    worker = plugin.get_workers()["default"]
    assert not worker._enable_heartbeat_manager  # pyright: ignore[reportPrivateUsage]
    app = Litestar(plugins=[plugin], openapi_config=None)
    assert app.plugins.get(SAQPlugin) is plugin


class CallbackRunner:
    async def run(self, operation: Callable[[Session], T]) -> T:
        return operation(cast("Session", object()))


class RecordingQueue:
    def __init__(self, result: object | None = object()) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def enqueue(self, job_or_func: str, /, **kwargs: object) -> object | None:
        self.calls.append((job_or_func, kwargs))
        return self.result


def claimed_message() -> ClaimedMessage:
    return ClaimedMessage(
        id=uuid4(),
        topic="task.created.v1",
        payload={"version": 1, "task_id": 7},
        attempt_count=1,
    )


async def test_worker_job_uses_non_blocking_200ms_delay_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    processed: list[int] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    def fake_process(_uow: object, _clock: object, *, task_id: int) -> object:
        processed.append(task_id)
        return {"task_id": task_id}

    monkeypatch.setattr(worker_module.anyio, "sleep", fake_sleep)
    monkeypatch.setattr(worker_module, "process_task_created", fake_process)
    resources = WorkerResources(
        engine=cast("AsyncEngine", object()),
        runner=cast("TransactionRunner", CallbackRunner()),
        clock=cast("Clock", FakeClock()),
        lease_duration=timedelta(seconds=30),
        batch_size=25,
    )
    context = cast("Context", {"app_resources": resources})
    first = await process_task_created_job(
        context, payload={"version": 1, "task_id": 7}
    )
    duplicate = await process_task_created_job(
        context, payload={"version": 1, "task_id": 7}
    )
    assert first == duplicate == {"task_id": 7, "processed": True}
    assert sleeps == [0.2, 0.2]
    assert processed == [7, 7]


async def test_duplicate_enqueue_none_is_successfully_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = claimed_message()
    acknowledgements: list[object] = []

    def fake_claim(
        _uow: UnitOfWork,
        _clock: Clock,
        *,
        owner: str,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[ClaimedMessage, ...]:
        del owner, lease_duration, limit
        return (message,)

    def fake_acknowledge(
        _uow: UnitOfWork,
        _clock: Clock,
        *,
        message_id: UUID,
        owner: str,
    ) -> bool:
        del owner
        acknowledgements.append(message_id)
        return True

    monkeypatch.setattr(
        relay_module,
        "claim_messages",
        fake_claim,
    )
    monkeypatch.setattr(
        relay_module,
        "acknowledge_message",
        fake_acknowledge,
    )
    queue = RecordingQueue(result=None)
    acknowledged = await relay_once(
        cast("TransactionRunner", CallbackRunner()),
        queue,
        FakeClock(),
        owner="worker-1",
        lease_duration=timedelta(seconds=30),
        batch_size=10,
    )
    assert acknowledged == 1
    assert acknowledgements == [message.id]
    assert queue.calls[0][1]["key"] == str(message.id)


async def test_enqueue_failure_records_safe_error_for_lease_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = claimed_message()
    failures: list[str] = []

    def fake_claim(
        _uow: UnitOfWork,
        _clock: Clock,
        *,
        owner: str,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[ClaimedMessage, ...]:
        del owner, lease_duration, limit
        return (message,)

    def fake_fail(
        _uow: UnitOfWork,
        *,
        message_id: UUID,
        owner: str,
        error: str,
    ) -> bool:
        del message_id, owner
        failures.append(error)
        return True

    monkeypatch.setattr(
        relay_module,
        "claim_messages",
        fake_claim,
    )
    monkeypatch.setattr(
        relay_module,
        "fail_message",
        fake_fail,
    )

    class FailingQueue:
        async def enqueue(
            self, job_or_func: str, /, **_kwargs: object
        ) -> object | None:
            del job_or_func
            raise RuntimeError("queue unavailable")

    acknowledged = await relay_once(
        cast("TransactionRunner", CallbackRunner()),
        FailingQueue(),
        FakeClock(),
        owner="worker-1",
        lease_duration=timedelta(seconds=30),
        batch_size=10,
    )
    assert acknowledged == 0
    assert failures == ["RuntimeError: queue unavailable"]


async def test_relay_reraises_cancellation_without_marking_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = claimed_message()
    failures: list[str] = []

    def fake_claim(
        _uow: UnitOfWork,
        _clock: Clock,
        *,
        owner: str,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[ClaimedMessage, ...]:
        del owner, lease_duration, limit
        return (message,)

    def fake_fail(
        _uow: UnitOfWork,
        *,
        message_id: UUID,
        owner: str,
        error: str,
    ) -> bool:
        del message_id, owner
        failures.append(error)
        return True

    monkeypatch.setattr(
        relay_module,
        "claim_messages",
        fake_claim,
    )
    monkeypatch.setattr(
        relay_module,
        "fail_message",
        fake_fail,
    )

    class CancelledQueue:
        async def enqueue(
            self, job_or_func: str, /, **_kwargs: object
        ) -> object | None:
            del job_or_func
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await relay_once(
            cast("TransactionRunner", CallbackRunner()),
            CancelledQueue(),
            FakeClock(),
            owner="worker-1",
            lease_duration=timedelta(seconds=30),
            batch_size=10,
        )
    assert failures == []
