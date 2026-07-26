"""Generic transactional outbox primitives."""

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """A durable message and its publication lease state."""

    id: UUID
    topic: str
    payload: dict[str, JsonValue]
    created_at: datetime
    published_at: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    last_error: str | None = None

    def leased(
        self, owner: str, expires_at: datetime, attempted_at: datetime
    ) -> "OutboxMessage":
        return replace(
            self,
            lease_owner=owner,
            lease_expires_at=expires_at,
            attempt_count=self.attempt_count + 1,
            last_error=None,
        )

    def published(self, published_at: datetime) -> "OutboxMessage":
        return replace(
            self,
            published_at=published_at,
            lease_owner=None,
            lease_expires_at=None,
            last_error=None,
        )

    def failed(self, error: str) -> "OutboxMessage":
        # Retain the lease so a hot failure cannot be reclaimed immediately.
        # The row becomes eligible again when the persisted lease expires.
        return replace(self, last_error=error)
