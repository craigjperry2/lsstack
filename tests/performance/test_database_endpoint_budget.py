from __future__ import annotations

from dataclasses import replace
from statistics import median
from time import perf_counter
from typing import TYPE_CHECKING

import pytest
from litestar.testing import TestClient

from app.main import create_app
from tests.integration.test_postgres_contracts import FastPasswordHasher

if TYPE_CHECKING:
    from collections.abc import Callable

    from httpx import Response
    from litestar import Litestar

    from tests.integration.conftest import DatabaseHarness


def _form(client: TestClient[Litestar], **values: str) -> dict[str, str]:
    return {"_csrf_token": client.cookies["lsstack_csrf"], **values}


def _timed(action: Callable[[], Response], expected: set[int]) -> float:
    started = perf_counter()
    response = action()
    elapsed = perf_counter() - started
    assert response.status_code in expected
    return elapsed


def _assert_budget(timings: list[float]) -> None:
    assert len(timings) >= 10
    assert median(timings) < 0.1


@pytest.mark.integration
@pytest.mark.performance
async def test_real_database_representative_handlers_stay_under_100ms(
    database_harness: DatabaseHarness,
) -> None:
    application = create_app(database_harness.settings)
    dependencies = application.state.web_dependencies
    application.state.web_dependencies = replace(
        dependencies,
        passwords=FastPasswordHasher(),
    )
    public_ids = dependencies.public_ids

    with TestClient(
        app=application,
        session_config=application.state.session_config,
    ) as client:
        client.get("/register")
        registration_timings: list[float] = []
        for index in range(10):
            registration_timings.append(
                _timed(
                    lambda index=index: client.post(
                        "/register",
                        data=_form(
                            client,
                            email=f"perf-{index}@example.com",
                            password="correct-horse-battery",
                            password_confirmation="correct-horse-battery",
                        ),
                        follow_redirects=False,
                    ),
                    {303},
                )
            )
            client.post("/logout", data=_form(client), follow_redirects=False)
        _assert_budget(registration_timings)

        login_timings: list[float] = []
        for _ in range(10):
            login_timings.append(
                _timed(
                    lambda: client.post(
                        "/login",
                        data=_form(
                            client,
                            email="perf-0@example.com",
                            password="correct-horse-battery",
                        ),
                        follow_redirects=False,
                    ),
                    {303},
                )
            )
            client.post("/logout", data=_form(client), follow_redirects=False)
        _assert_budget(login_timings)

        client.post(
            "/login",
            data=_form(
                client,
                email="perf-0@example.com",
                password="correct-horse-battery",
            ),
            follow_redirects=False,
        )
        create_timings = [
            _timed(
                lambda index=index: client.post(
                    "/tasks",
                    data=_form(client, title=f"Measured task {index}"),
                    headers={"HX-Request": "true"},
                ),
                {200},
            )
            for index in range(10)
        ]
        first_task = public_ids.encode(1)
        list_timings = [_timed(lambda: client.get("/tasks"), {200}) for _ in range(10)]
        edit_timings = [
            _timed(
                lambda index=index: client.post(
                    f"/tasks/{first_task}/edit",
                    data=_form(client, title=f"Edited task {index}"),
                    headers={"HX-Request": "true"},
                ),
                {200},
            )
            for index in range(10)
        ]
        toggle_timings = [
            _timed(
                lambda: client.post(
                    f"/tasks/{first_task}/toggle",
                    data=_form(client),
                    headers={"HX-Request": "true"},
                ),
                {200},
            )
            for _ in range(10)
        ]
        status_timings = [
            _timed(lambda: client.get(f"/tasks/{first_task}/status"), {200})
            for _ in range(10)
        ]
        delete_timings = [
            _timed(
                lambda task_id=task_id: client.post(
                    f"/tasks/{public_ids.encode(task_id)}/delete",
                    data=_form(client),
                    headers={"HX-Request": "true"},
                ),
                {200},
            )
            for task_id in range(1, 11)
        ]

    for timings in (
        create_timings,
        list_timings,
        edit_timings,
        toggle_timings,
        status_timings,
        delete_timings,
    ):
        _assert_budget(timings)
