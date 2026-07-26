from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

import pytest
from litestar import Litestar
from litestar.config.csrf import CSRFConfig
from litestar.datastructures import State
from litestar.middleware.session.client_side import CookieBackendConfig
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient

from app.adapters.web.dependencies import WebDependencies
from app.adapters.web.handlers import ROUTE_HANDLERS
from app.adapters.web.middleware import RequestEdgeMiddleware
from app.application.ports.security import PasswordVerification
from app.domain.errors import DuplicateEmailError
from app.domain.tasks import Task
from app.domain.users import User

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy.orm import Session

    from app.domain.outbox import OutboxMessage

T = TypeVar("T")
TEMPLATES = Path(__file__).parents[1] / "src" / "app" / "templates"


@dataclass
class FakeClock:
    value: datetime = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class FakePasswordHasher:
    def hash(self, password: str) -> str:
        return f"hash:{password}"

    def verify(self, password: str, password_hash: str) -> PasswordVerification:
        return PasswordVerification(password_hash == self.hash(password))

    def verify_unknown(self, password: str) -> None:
        self.verify(password, "hash:not-the-password")


class FakePublicIds:
    def encode(self, internal_id: int) -> str:
        if internal_id < 1:
            raise ValueError
        return f"task{internal_id:04d}"

    def decode(self, public_id: str) -> int | None:
        if not public_id.startswith("task") or not public_id[4:].isdigit():
            return None
        value = int(public_id[4:])
        return value if value > 0 and self.encode(value) == public_id else None


class FakeUserRepository:
    def __init__(self) -> None:
        self.values: dict[int, User] = {}

    def add(self, user: User) -> User:
        if any(
            existing.email_normalized == user.email_normalized
            for existing in self.values.values()
        ):
            raise DuplicateEmailError
        stored = User(
            id=len(self.values) + 1,
            email_normalized=user.email_normalized,
            password_hash=user.password_hash,
            session_version=user.session_version,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )
        self.values[cast("int", stored.id)] = stored
        return stored

    def get_by_id(self, user_id: int) -> User | None:
        return self.values.get(user_id)

    def get_by_email(self, email_normalized: str) -> User | None:
        return next(
            (
                user
                for user in self.values.values()
                if user.email_normalized == email_normalized
            ),
            None,
        )

    def update(self, user: User) -> User:
        assert user.id is not None
        self.values[user.id] = user
        return user


class FakeTaskRepository:
    def __init__(self) -> None:
        self.values: dict[int, Task] = {}

    def add(self, task: Task) -> Task:
        stored = Task(
            id=len(self.values) + 1,
            user_id=task.user_id,
            title=task.title,
            description=task.description,
            is_completed=task.is_completed,
            background_processed_at=task.background_processed_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
        self.values[cast("int", stored.id)] = stored
        return stored

    def get_owned(self, task_id: int, user_id: int) -> Task | None:
        task = self.values.get(task_id)
        return task if task is not None and task.user_id == user_id else None

    def list_owned(self, user_id: int) -> tuple[Task, ...]:
        return tuple(
            reversed([task for task in self.values.values() if task.user_id == user_id])
        )

    def update(self, task: Task) -> Task:
        assert task.id is not None
        self.values[task.id] = task
        return task

    def delete_owned(self, task_id: int, user_id: int) -> bool:
        if self.get_owned(task_id, user_id) is None:
            return False
        del self.values[task_id]
        return True

    def mark_background_processed(
        self, task_id: int, processed_at: datetime
    ) -> Task | None:
        task = self.values.get(task_id)
        if task is None:
            return None
        stored = task.with_background_processed(processed_at)
        self.values[task_id] = stored
        return stored


class FakeOutboxRepository:
    def __init__(self) -> None:
        self.values: dict[object, OutboxMessage] = {}

    def add(self, message: OutboxMessage) -> OutboxMessage:
        self.values[message.id] = message
        return message

    def claim_batch(
        self,
        *,
        owner: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[OutboxMessage, ...]:
        eligible = [
            message
            for message in self.values.values()
            if message.published_at is None
            and (message.lease_expires_at is None or message.lease_expires_at <= now)
        ][:limit]
        claimed = tuple(
            message.leased(owner, lease_expires_at, now) for message in eligible
        )
        self.values.update({message.id: message for message in claimed})
        return claimed

    def mark_published(
        self, message_id: object, owner: str, published_at: datetime
    ) -> bool:
        message = self.values.get(message_id)
        if message is None or message.lease_owner != owner:
            return False
        self.values[message_id] = message.published(published_at)
        return True

    def mark_failed(self, message_id: object, owner: str, error: str) -> bool:
        message = self.values.get(message_id)
        if message is None or message.lease_owner != owner:
            return False
        self.values[message_id] = message.failed(error)
        return True


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.users = FakeUserRepository()
        self.tasks = FakeTaskRepository()
        self.outbox = FakeOutboxRepository()


class FakeTransactionRunner:
    def __init__(self, uow: FakeUnitOfWork) -> None:
        self.uow = uow
        self.run_calls = 0

    async def run(self, operation: Callable[[Session], T]) -> T:
        self.run_calls += 1
        return operation(cast("Session", object()))

    async def probe(self) -> bool:
        return True


@dataclass(frozen=True)
class WebHarness:
    client: TestClient[Litestar]
    app: Litestar
    uow: FakeUnitOfWork
    transactions: FakeTransactionRunner
    clock: FakeClock
    csrf_cookie_name: str = "lsstack_csrf"

    def csrf(self) -> str:
        return self.client.cookies[self.csrf_cookie_name]

    def form(self, **values: str) -> dict[str, str]:
        return {"_csrf_token": self.csrf(), **values}


@pytest.fixture
def web_harness() -> Iterator[WebHarness]:
    uow = FakeUnitOfWork()
    clock = FakeClock()
    transactions = FakeTransactionRunner(uow)
    session_config = CookieBackendConfig(
        secret=b"test-session-key-is-32-bytes!!!!",
        key="lsstack_session",
        max_age=43_200,
        secure=False,
        httponly=True,
        samesite="lax",
    )
    app = Litestar(
        route_handlers=list(ROUTE_HANDLERS),
        middleware=[RequestEdgeMiddleware, session_config.middleware],
        csrf_config=CSRFConfig(
            secret="test-csrf-secret-with-sufficient-entropy",
            cookie_name="lsstack_csrf",
        ),
        template_config=TemplateConfig(
            directory=TEMPLATES,
            engine=JinjaTemplateEngine,
        ),
        state=State(
            {
                "web_dependencies": WebDependencies(
                    transactions=transactions,
                    unit_of_work=lambda _: uow,
                    passwords=FakePasswordHasher(),
                    clock=clock,
                    public_ids=FakePublicIds(),
                    session_lifetime_seconds=43_200,
                )
            }
        ),
        openapi_config=None,
    )
    with TestClient(app=app, session_config=session_config) as client:
        yield WebHarness(
            client=client,
            app=app,
            uow=uow,
            transactions=transactions,
            clock=clock,
        )
