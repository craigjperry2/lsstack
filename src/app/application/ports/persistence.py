"""Synchronous repository and unit-of-work ports."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.outbox import OutboxMessage
from app.domain.tasks import Task
from app.domain.users import User


class UserRepository(Protocol):
    def add(self, user: User) -> User: ...

    def get_by_id(self, user_id: int) -> User | None: ...

    def get_by_email(self, email_normalized: str) -> User | None: ...

    def update(self, user: User) -> User: ...


class TaskRepository(Protocol):
    def add(self, task: Task) -> Task: ...

    def get_owned(self, task_id: int, user_id: int) -> Task | None: ...

    def list_owned(self, user_id: int) -> tuple[Task, ...]: ...

    def update(self, task: Task) -> Task: ...

    def delete_owned(self, task_id: int, user_id: int) -> bool: ...

    def mark_background_processed(
        self, task_id: int, processed_at: datetime
    ) -> Task | None: ...


class OutboxRepository(Protocol):
    def add(self, message: OutboxMessage) -> OutboxMessage: ...

    def claim_batch(
        self,
        *,
        owner: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[OutboxMessage, ...]: ...

    def mark_published(
        self, message_id: UUID, owner: str, published_at: datetime
    ) -> bool: ...

    def mark_failed(self, message_id: UUID, owner: str, error: str) -> bool: ...


class UnitOfWork(Protocol):
    """Repositories sharing the transaction owned by the outer adapter."""

    @property
    def users(self) -> UserRepository: ...

    @property
    def tasks(self) -> TaskRepository: ...

    @property
    def outbox(self) -> OutboxRepository: ...
