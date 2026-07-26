"""At-least-once outbox publication across short transactions."""

import asyncio
from datetime import timedelta
from typing import Protocol

import structlog
from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode

from app.adapters.persistence import SqlAlchemyUnitOfWork, TransactionRunner
from app.application.outbox import (
    ClaimedMessage,
    acknowledge_message,
    claim_messages,
    fail_message,
)
from app.application.ports.clock import Clock

TASK_CREATED_JOB = "process_task_created_job"


class SaqQueue(Protocol):
    async def enqueue(self, job_or_func: str, /, **kwargs: object) -> object | None: ...


def _job_name(topic: str) -> str:
    if topic == "task.created.v1":
        return TASK_CREATED_JOB
    raise ValueError(f"No SAQ handler is registered for outbox topic {topic!r}.")


async def publish_message(queue: SaqQueue, message: ClaimedMessage) -> None:
    """Publish with the outbox UUID as SAQ's stable idempotency key."""
    tracer = trace.get_tracer("app.outbox")
    with tracer.start_as_current_span(
        "outbox.publish",
        attributes={
            "messaging.message.id": str(message.id),
            "messaging.destination.name": message.topic,
            "outbox.attempt": message.attempt_count,
        },
    ) as span:
        try:
            await queue.enqueue(
                _job_name(message.topic),
                payload=message.payload,
                key=str(message.id),
                retries=3,
                retry_delay=1.0,
                retry_backoff=True,
                timeout=30,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            span.set_attribute("error.type", type(error).__name__)
            span.set_status(Status(StatusCode.ERROR))
            raise
        else:
            metrics.get_meter("app.outbox").create_counter(
                "outbox_messages_published",
                unit="{message}",
            ).add(1, {"messaging.destination.name": message.topic})
    # SAQ returns None when the key already exists. That is intentionally success.


async def relay_once(
    runner: TransactionRunner,
    queue: SaqQueue,
    clock: Clock,
    *,
    owner: str,
    lease_duration: timedelta,
    batch_size: int,
) -> int:
    """Claim, publish, and acknowledge one bounded batch."""
    claimed = await runner.run(
        lambda session: claim_messages(
            SqlAlchemyUnitOfWork(session),
            clock,
            owner=owner,
            lease_duration=lease_duration,
            limit=batch_size,
        )
    )
    logger = structlog.get_logger("app.outbox")
    logger.info(
        "outbox_batch_claimed",
        message_count=len(claimed),
        lease_owner=owner,
    )
    acknowledged = 0
    for message in claimed:
        try:
            await publish_message(queue, message)
            published = await runner.run(
                lambda session, message=message: acknowledge_message(
                    SqlAlchemyUnitOfWork(session),
                    clock,
                    message_id=message.id,
                    owner=owner,
                )
            )
            acknowledged += int(published)
            logger.info(
                "outbox_message_acknowledged",
                message_id=str(message.id),
                topic=message.topic,
                attempt_count=message.attempt_count,
                acknowledged=published,
            )
            metrics.get_meter("app.outbox").create_counter(
                "outbox_messages_acknowledged",
                unit="{message}",
            ).add(
                int(published),
                {"messaging.destination.name": message.topic},
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "outbox_message_publication_failed",
                message_id=str(message.id),
                topic=message.topic,
                attempt_count=message.attempt_count,
                error_type=type(error).__name__,
            )
            metrics.get_meter("app.outbox").create_counter(
                "outbox_message_failures",
                unit="{message}",
            ).add(1, {"messaging.destination.name": message.topic})
            # Preserve the lease so retries do not hammer a failing queue.
            await runner.run(
                lambda session, message=message, error=error: fail_message(
                    SqlAlchemyUnitOfWork(session),
                    message_id=message.id,
                    owner=owner,
                    error=f"{type(error).__name__}: {error}",
                )
            )
    return acknowledged
