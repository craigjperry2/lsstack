"""User-owned task CRUD and the idempotent background effect."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from app.application.ports.clock import Clock
from app.application.ports.persistence import UnitOfWork
from app.domain.errors import TaskNotFoundError
from app.domain.outbox import OutboxMessage
from app.domain.tasks import Task, normalize_description, normalize_title

TASK_CREATED_TOPIC = "task.created.v1"


@dataclass(frozen=True, slots=True)
class TaskResult:
    id: int
    user_id: int
    title: str
    description: str | None
    is_completed: bool
    background_processed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskCreatedResult:
    task: TaskResult
    outbox_message_id: UUID


def _result(task: Task) -> TaskResult:
    if task.id is None:
        raise RuntimeError("The task repository returned an unpersisted task.")
    return TaskResult(
        id=task.id,
        user_id=task.user_id,
        title=task.title,
        description=task.description,
        is_completed=task.is_completed,
        background_processed_at=task.background_processed_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def list_tasks(uow: UnitOfWork, *, user_id: int) -> tuple[TaskResult, ...]:
    return tuple(_result(task) for task in uow.tasks.list_owned(user_id))


def create_task(
    uow: UnitOfWork,
    clock: Clock,
    *,
    user_id: int,
    title: str,
    description: str | None = None,
    message_id: UUID | None = None,
) -> TaskCreatedResult:
    """Persist a task and generic outbox message in the caller's transaction."""
    now = clock.now()
    task = uow.tasks.add(
        Task(
            id=None,
            user_id=user_id,
            title=normalize_title(title),
            description=normalize_description(description),
            is_completed=False,
            background_processed_at=None,
            created_at=now,
            updated_at=now,
        )
    )
    task_result = _result(task)
    outbox_id = message_id or uuid4()
    uow.outbox.add(
        OutboxMessage(
            id=outbox_id,
            topic=TASK_CREATED_TOPIC,
            payload={"version": 1, "task_id": task_result.id},
            created_at=now,
        )
    )
    return TaskCreatedResult(task_result, outbox_id)


def get_task(uow: UnitOfWork, *, user_id: int, task_id: int) -> TaskResult:
    task = uow.tasks.get_owned(task_id, user_id)
    if task is None:
        raise TaskNotFoundError("Task not found.")
    return _result(task)


def update_task(
    uow: UnitOfWork,
    clock: Clock,
    *,
    user_id: int,
    task_id: int,
    title: str,
    description: str | None,
) -> TaskResult:
    task = uow.tasks.get_owned(task_id, user_id)
    if task is None:
        raise TaskNotFoundError("Task not found.")
    return _result(uow.tasks.update(task.edited(title, description, clock.now())))


def toggle_task(
    uow: UnitOfWork, clock: Clock, *, user_id: int, task_id: int
) -> TaskResult:
    task = uow.tasks.get_owned(task_id, user_id)
    if task is None:
        raise TaskNotFoundError("Task not found.")
    return _result(
        uow.tasks.update(
            task.with_completion(
                completed=not task.is_completed, changed_at=clock.now()
            )
        )
    )


def delete_task(uow: UnitOfWork, *, user_id: int, task_id: int) -> None:
    if not uow.tasks.delete_owned(task_id, user_id):
        raise TaskNotFoundError("Task not found.")


def process_task_created(
    uow: UnitOfWork, clock: Clock, *, task_id: int
) -> TaskResult | None:
    """Apply the example worker effect once; duplicates safely become reads."""
    task = uow.tasks.mark_background_processed(task_id, clock.now())
    return None if task is None else _result(task)
