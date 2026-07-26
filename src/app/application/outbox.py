"""Short transactional outbox lease and acknowledgement use cases."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from app.application.ports.clock import Clock
from app.application.ports.persistence import UnitOfWork
from app.domain.outbox import JsonValue


@dataclass(frozen=True, slots=True)
class ClaimedMessage:
    id: UUID
    topic: str
    payload: dict[str, JsonValue]
    attempt_count: int


def claim_messages(
    uow: UnitOfWork,
    clock: Clock,
    *,
    owner: str,
    lease_duration: timedelta,
    limit: int = 25,
) -> tuple[ClaimedMessage, ...]:
    if not owner:
        raise ValueError("A lease owner is required.")
    if lease_duration <= timedelta(0):
        raise ValueError("Lease duration must be positive.")
    if limit < 1 or limit > 1_000:
        raise ValueError("Outbox claim limit must be between 1 and 1000.")
    now = clock.now()
    claimed = uow.outbox.claim_batch(
        owner=owner,
        now=now,
        lease_expires_at=now + lease_duration,
        limit=limit,
    )
    return tuple(
        ClaimedMessage(
            message.id,
            message.topic,
            message.payload,
            message.attempt_count,
        )
        for message in claimed
    )


def acknowledge_message(
    uow: UnitOfWork, clock: Clock, *, message_id: UUID, owner: str
) -> bool:
    return uow.outbox.mark_published(message_id, owner, clock.now())


def fail_message(uow: UnitOfWork, *, message_id: UUID, owner: str, error: str) -> bool:
    safe_error = error.strip()[:2_000] or "Queue publication failed."
    return uow.outbox.mark_failed(message_id, owner, safe_error)
