"""Synchronous SQLAlchemy repositories used only inside run_sync()."""

from collections.abc import Mapping
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Select, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    OutboxMessageModel,
    TaskModel,
    UserModel,
)
from app.application.ports.persistence import (
    OutboxRepository,
    TaskRepository,
    UserRepository,
)
from app.domain.errors import DuplicateEmailError
from app.domain.outbox import JsonValue, OutboxMessage
from app.domain.tasks import Task
from app.domain.users import User


def _user(model: UserModel) -> User:
    return User(
        id=model.id,
        email_normalized=model.email_normalized,
        password_hash=model.password_hash,
        session_version=model.session_version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _task(model: TaskModel) -> Task:
    return Task(
        id=model.id,
        user_id=model.user_id,
        title=model.title,
        description=model.description,
        is_completed=model.is_completed,
        background_processed_at=model.background_processed_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _payload(value: Mapping[str, object]) -> dict[str, JsonValue]:
    # JSONB values have already passed through SQLAlchemy's JSON serializer.
    return cast("dict[str, JsonValue]", dict(value))


def _outbox(model: OutboxMessageModel) -> OutboxMessage:
    return OutboxMessage(
        id=model.id,
        topic=model.topic,
        payload=_payload(model.payload),
        created_at=model.created_at,
        published_at=model.published_at,
        lease_owner=model.lease_owner,
        lease_expires_at=model.lease_expires_at,
        attempt_count=model.attempt_count,
        last_error=model.last_error,
    )


class SqlAlchemyUserRepository(UserRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, user: User) -> User:
        model = UserModel(
            email_normalized=user.email_normalized,
            password_hash=user.password_hash,
            session_version=user.session_version,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )
        self._session.add(model)
        try:
            self._session.flush()
        except IntegrityError as error:
            if "email" in str(error.orig).casefold():
                raise DuplicateEmailError(
                    "That email is already registered."
                ) from error
            raise
        return _user(model)

    def get_by_id(self, user_id: int) -> User | None:
        model = self._session.get(UserModel, user_id)
        return None if model is None else _user(model)

    def get_by_id_for_update(self, user_id: int) -> User | None:
        model = self._session.scalar(
            select(UserModel)
            .where(UserModel.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return None if model is None else _user(model)

    def get_by_email_for_update(self, email_normalized: str) -> User | None:
        model = self._session.scalar(
            select(UserModel)
            .where(UserModel.email_normalized == email_normalized)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return None if model is None else _user(model)

    def update_credentials(self, user: User) -> User:
        if user.id is None:
            raise ValueError("Cannot update an unpersisted user.")
        model = self._session.get(UserModel, user.id)
        if model is None:
            raise ValueError("Cannot update a missing user.")
        model.password_hash = user.password_hash
        model.session_version = user.session_version
        model.updated_at = user.updated_at
        self._session.flush()
        return _user(model)


class SqlAlchemyTaskRepository(TaskRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, task: Task) -> Task:
        model = TaskModel(
            user_id=task.user_id,
            title=task.title,
            description=task.description,
            is_completed=task.is_completed,
            background_processed_at=task.background_processed_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
        self._session.add(model)
        self._session.flush()
        return _task(model)

    @staticmethod
    def _owned_query(task_id: int, user_id: int) -> Select[tuple[TaskModel]]:
        return select(TaskModel).where(
            TaskModel.id == task_id, TaskModel.user_id == user_id
        )

    def get_owned(self, task_id: int, user_id: int) -> Task | None:
        model = self._session.scalar(self._owned_query(task_id, user_id))
        return None if model is None else _task(model)

    def list_owned(self, user_id: int) -> tuple[Task, ...]:
        models = self._session.scalars(
            select(TaskModel)
            .where(TaskModel.user_id == user_id)
            .order_by(TaskModel.created_at.desc(), TaskModel.id.desc())
        )
        return tuple(_task(model) for model in models)

    def update(self, task: Task) -> Task:
        if task.id is None:
            raise ValueError("Cannot update an unpersisted task.")
        model = self._session.scalar(self._owned_query(task.id, task.user_id))
        if model is None:
            raise ValueError("Cannot update a missing or unowned task.")
        model.title = task.title
        model.description = task.description
        model.is_completed = task.is_completed
        model.background_processed_at = task.background_processed_at
        model.updated_at = task.updated_at
        self._session.flush()
        return _task(model)

    def delete_owned(self, task_id: int, user_id: int) -> bool:
        deleted_id = self._session.scalar(
            delete(TaskModel)
            .where(TaskModel.id == task_id, TaskModel.user_id == user_id)
            .returning(TaskModel.id)
        )
        return deleted_id is not None

    def mark_background_processed(
        self, task_id: int, processed_at: datetime
    ) -> Task | None:
        self._session.execute(
            update(TaskModel)
            .where(
                TaskModel.id == task_id,
                TaskModel.background_processed_at.is_(None),
            )
            .values(
                background_processed_at=processed_at,
                updated_at=processed_at,
            )
        )
        model = self._session.get(TaskModel, task_id)
        return None if model is None else _task(model)


class SqlAlchemyOutboxRepository(OutboxRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, message: OutboxMessage) -> OutboxMessage:
        model = OutboxMessageModel(
            id=message.id,
            topic=message.topic,
            payload=cast("dict[str, object]", message.payload),
            created_at=message.created_at,
            published_at=message.published_at,
            lease_owner=message.lease_owner,
            lease_expires_at=message.lease_expires_at,
            attempt_count=message.attempt_count,
            last_error=message.last_error,
        )
        self._session.add(model)
        self._session.flush()
        return _outbox(model)

    def claim_batch(
        self,
        *,
        owner: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[OutboxMessage, ...]:
        models = self._session.scalars(
            select(OutboxMessageModel)
            .where(
                OutboxMessageModel.published_at.is_(None),
                or_(
                    OutboxMessageModel.lease_expires_at.is_(None),
                    OutboxMessageModel.lease_expires_at <= now,
                ),
            )
            .order_by(OutboxMessageModel.created_at.asc(), OutboxMessageModel.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for model in models:
            model.lease_owner = owner
            model.lease_expires_at = lease_expires_at
            model.attempt_count += 1
            model.last_error = None
        self._session.flush()
        return tuple(_outbox(model) for model in models)

    def mark_published(
        self, message_id: UUID, owner: str, published_at: datetime
    ) -> bool:
        published_id = self._session.scalar(
            update(OutboxMessageModel)
            .where(
                OutboxMessageModel.id == message_id,
                OutboxMessageModel.lease_owner == owner,
                OutboxMessageModel.published_at.is_(None),
            )
            .values(
                published_at=published_at,
                lease_owner=None,
                lease_expires_at=None,
                last_error=None,
            )
            .returning(OutboxMessageModel.id)
        )
        return published_id is not None

    def mark_failed(self, message_id: UUID, owner: str, error: str) -> bool:
        failed_id = self._session.scalar(
            update(OutboxMessageModel)
            .where(
                OutboxMessageModel.id == message_id,
                OutboxMessageModel.lease_owner == owner,
                OutboxMessageModel.published_at.is_(None),
            )
            .values(last_error=error)
            .returning(OutboxMessageModel.id)
        )
        return failed_id is not None


class SqlAlchemyUnitOfWork:
    """Repository bundle over one synchronous SQLAlchemy transaction proxy."""

    def __init__(self, session: Session) -> None:
        self._users = SqlAlchemyUserRepository(session)
        self._tasks = SqlAlchemyTaskRepository(session)
        self._outbox = SqlAlchemyOutboxRepository(session)

    @property
    def users(self) -> SqlAlchemyUserRepository:
        return self._users

    @property
    def tasks(self) -> SqlAlchemyTaskRepository:
        return self._tasks

    @property
    def outbox(self) -> SqlAlchemyOutboxRepository:
        return self._outbox
