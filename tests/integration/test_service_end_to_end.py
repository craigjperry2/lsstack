from __future__ import annotations

import os
import re
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

import anyio
import httpx
import pytest
from sqlalchemy import select

from app.adapters.persistence import (
    TransactionRunner,
    create_async_session_factory,
)
from app.adapters.persistence.engine import create_engine
from app.adapters.persistence.models import OutboxMessageModel, TaskModel, UserModel
from app.config import load_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

CSRF_INPUT = re.compile(r'name="_csrf_token"[^>]*value="([^"]+)"')


def csrf_token(html: str) -> str:
    match = CSRF_INPUT.search(html)
    if match is None:
        raise AssertionError("Response did not contain a CSRF form token.")
    return match.group(1)


def task_row(html: str, title: str) -> str | None:
    title_position = html.find(title)
    if title_position < 0:
        return None
    start = html.rfind('<article class="task-row"', 0, title_position)
    end = html.find("</article>", title_position)
    if start < 0 or end < 0:
        return None
    return html[start : end + len("</article>")]


@pytest.mark.integration
async def test_nginx_routing_request_ids_headers_and_static_cache_policy() -> None:
    if os.environ.get("RUN_SERVICE_TESTS") != "1":
        pytest.skip("Set RUN_SERVICE_TESTS=1 after starting Nginx, app, and worker.")

    settings = load_settings(env_file=None)
    async with httpx.AsyncClient(
        base_url=settings.public_base_url,
        timeout=5,
    ) as client:
        dynamic = await client.get(
            "/livez?private=must-not-affect-routing",
            headers={"X-Request-ID": "safe-request_123"},
        )
        assert dynamic.status_code == 200
        assert dynamic.headers["x-request-id"] == "safe-request_123"
        assert dynamic.headers["cache-control"] == "no-store"
        assert dynamic.headers["x-content-type-options"] == "nosniff"
        assert dynamic.headers["x-frame-options"] == "DENY"
        assert "unsafe-inline" not in dynamic.headers["content-security-policy"]

        invalid = await client.get("/livez", headers={"X-Request-ID": "---"})
        generated = invalid.headers["x-request-id"]
        assert generated != "---"
        assert len(generated) == 32

        static = await client.get("/static/app-v3.css")
        assert static.status_code == 200
        assert static.headers["content-type"].startswith("text/css")
        assert static.headers["cache-control"] == (
            "public, max-age=31536000, immutable"
        )
        assert "set-cookie" not in static.headers


@pytest.mark.integration
async def test_task_reaches_real_worker_and_persists_processed_timestamp() -> None:
    if os.environ.get("RUN_SERVICE_TESTS") != "1":
        pytest.skip("Set RUN_SERVICE_TESTS=1 after starting Nginx, app, and worker.")

    settings = load_settings(env_file=None)
    unique = uuid4().hex
    email = f"service-{unique}@example.com"
    title = f"Service task {unique}"
    async with httpx.AsyncClient(
        base_url=settings.public_base_url,
        follow_redirects=False,
        timeout=5,
    ) as client:
        register_page = await client.get("/register")
        assert register_page.status_code == 200
        response = await client.post(
            "/register",
            data={
                "_csrf_token": csrf_token(register_page.text),
                "email": email,
                "password": "correct-horse-battery",
                "password_confirmation": "correct-horse-battery",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/tasks"

        tasks_page = await client.get("/tasks")
        response = await client.post(
            "/tasks",
            data={
                "_csrf_token": csrf_token(tasks_page.text),
                "title": title,
                "description": "End-to-end service smoke",
            },
        )
        assert response.status_code == 303

        deadline = monotonic() + 15
        while True:
            tasks_page = await client.get("/tasks")
            rendered_task = task_row(tasks_page.text, title)
            if rendered_task is not None and "Processed" in rendered_task:
                break
            if monotonic() >= deadline:
                pytest.fail(
                    "Task did not reach the processed state before the deadline."
                )
            await anyio.sleep(0.2)

    engine = create_engine(settings.database_url)
    runner = TransactionRunner(create_async_session_factory(engine))
    try:

        def persisted_state(
            session: Session,
        ) -> tuple[object, object, object]:
            task = session.scalar(
                select(TaskModel)
                .join(UserModel, TaskModel.user_id == UserModel.id)
                .where(UserModel.email_normalized == email, TaskModel.title == title)
            )
            assert task is not None
            outbox = session.scalar(
                select(OutboxMessageModel).where(
                    OutboxMessageModel.payload["task_id"].as_integer() == task.id
                )
            )
            assert outbox is not None
            return (
                task.background_processed_at,
                outbox.published_at,
                outbox.last_error,
            )

        processed_at, published_at, last_error = await runner.run(persisted_state)
        assert processed_at is not None
        assert published_at is not None
        assert last_error is None
    finally:
        await engine.dispose()
