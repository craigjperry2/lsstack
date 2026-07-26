"""Worker-owned resources, periodic relay, and versioned SAQ task."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import anyio
from litestar_saq import CronJob, QueueConfig, SAQConfig, SAQPlugin
from opentelemetry import trace
from opentelemetry.instrumentation.sqlalchemy import (  # pyright: ignore[reportMissingTypeStubs]
    SQLAlchemyInstrumentor,
)

from app.adapters.persistence import (
    SqlAlchemyUnitOfWork,
    TransactionRunner,
    create_async_session_factory,
)
from app.adapters.persistence.engine import create_engine
from app.adapters.queue.relay import relay_once
from app.adapters.security.clock import SystemClock
from app.application.tasks import process_task_created
from app.config import Settings, load_settings

if TYPE_CHECKING:
    from saq.types import Context, PartialTimersDict
    from saq.worker import Worker
    from sqlalchemy.ext.asyncio import AsyncEngine

    from app.application.ports.clock import Clock


@dataclass(slots=True)
class WorkerResources:
    engine: AsyncEngine
    runner: TransactionRunner
    clock: Clock
    lease_duration: timedelta
    batch_size: int
    sqlalchemy_instrumentor: SQLAlchemyInstrumentor | None = None


def _state(context: Context) -> dict[str, object]:
    return cast("dict[str, object]", context)


def _resources(context: Context) -> WorkerResources:
    resources = _state(context).get("app_resources")
    if not isinstance(resources, WorkerResources):
        raise TypeError("SAQ application resources were not initialized.")
    return resources


async def worker_startup(context: Context) -> None:
    settings = load_settings()
    engine = create_engine(settings.database_url, echo=False)
    instrumentor = None
    if settings.telemetry_enabled:
        instrumentor = SQLAlchemyInstrumentor()
        # The web engine is unused in a dedicated worker process.
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        instrumentor.instrument(
            engine=engine.sync_engine,
            tracer_provider=trace.get_tracer_provider(),
            enable_commenter=False,
        )
    _state(context)["app_resources"] = WorkerResources(
        engine=engine,
        runner=TransactionRunner(
            create_async_session_factory(engine),
            cleanup_timeout_seconds=settings.transaction_cleanup_timeout_seconds,
        ),
        clock=SystemClock(),
        lease_duration=timedelta(seconds=settings.outbox_lease_seconds),
        batch_size=settings.outbox_batch_size,
        sqlalchemy_instrumentor=instrumentor,
    )


async def worker_shutdown(context: Context) -> None:
    resources = _state(context).pop("app_resources", None)
    if not isinstance(resources, WorkerResources):
        return
    if resources.sqlalchemy_instrumentor is not None:
        resources.sqlalchemy_instrumentor.uninstrument()
    with anyio.move_on_after(5, shield=True):
        await resources.engine.dispose()


async def relay_outbox_job(context: Context) -> int:
    resources = _resources(context)
    worker = cast("Worker[Context]", context["worker"])
    queue = worker.queue
    return await relay_once(
        resources.runner,
        queue,
        resources.clock,
        owner=worker.id,
        lease_duration=resources.lease_duration,
        batch_size=resources.batch_size,
    )


async def process_task_created_job(
    context: Context, *, payload: object
) -> dict[str, object]:
    """Validate, delay asynchronously, and idempotently mark the task processed."""
    if not isinstance(payload, dict):
        raise TypeError("task.created.v1 payload must be an object.")
    message = cast("dict[str, object]", payload)
    version = message.get("version")
    task_id = message.get("task_id")
    if (
        isinstance(version, bool)
        or version != 1
        or not isinstance(task_id, int)
        or isinstance(task_id, bool)
        or task_id < 1
    ):
        raise ValueError("Invalid task.created.v1 payload.")
    # Context is retrieved after validation so malformed jobs perform no DB work.
    resources = _resources(context)
    await anyio.sleep(0.2)
    result = await resources.runner.run(
        lambda session: process_task_created(
            SqlAlchemyUnitOfWork(session), resources.clock, task_id=task_id
        )
    )
    return {"task_id": task_id, "processed": result is not None}


def build_saq_plugin(settings: Settings) -> SAQPlugin:
    """Build the one worker configuration exposed through litestar-saq."""
    queue_config = QueueConfig(
        name="default",
        dsn=settings.saq_database_url,
        tasks=[process_task_created_job, relay_outbox_job],
        scheduled_tasks=[
            CronJob(
                relay_outbox_job,
                cron="* * * * * */2",
                unique=True,
                timeout=30,
                retries=3,
            )
        ],
        startup=[worker_startup],
        shutdown=[worker_shutdown],
        timers=cast("PartialTimersDict", {"worker_info": 4, "sweep": 4}),
        concurrency=10,
        dequeue_timeout=1,
        separate_process=True,
        shutdown_grace_period_s=5,
        cancellation_hard_deadline_s=2,
        broker_options={"min_size": 1, "max_size": 10},
    )
    plugin = SAQPlugin(
        config=SAQConfig(
            queue_configs=[queue_config],
            use_server_lifespan=False,
            web_enabled=False,
            enable_otel=settings.telemetry_enabled,
        )
    )
    # litestar-saq 0.8 does not expose Worker's heartbeat switch through
    # QueueConfig. The worker runs in a separate CLI process, so prevent its
    # otherwise-idle heartbeat manager from starting in the web process.
    for worker in plugin.get_workers().values():
        worker._enable_heartbeat_manager = False  # pyright: ignore[reportPrivateUsage]
    return plugin
