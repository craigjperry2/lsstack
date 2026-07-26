from __future__ import annotations

from statistics import median
from time import perf_counter
from typing import TYPE_CHECKING

import pytest

from tests.integration.test_web_flows import register

if TYPE_CHECKING:
    from collections.abc import Callable

    from httpx import Response

    from tests.conftest import WebHarness


def _timed(action: Callable[[], Response], expected: set[int]) -> float:
    started = perf_counter()
    response = action()
    elapsed = perf_counter() - started
    assert response.status_code in expected
    return elapsed


def _assert_budget(timings: list[float]) -> None:
    assert len(timings) >= 10
    assert median(timings) < 0.1


@pytest.mark.performance
def test_registration_and_login_handlers_stay_under_budget(
    web_harness: WebHarness,
) -> None:
    web_harness.client.get("/register")
    registration_timings: list[float] = []
    for index in range(10):
        registration_timings.append(
            _timed(
                lambda index=index: web_harness.client.post(
                    "/register",
                    data=web_harness.form(
                        email=f"person-{index}@example.com",
                        password="correct-horse-battery",
                        password_confirmation="correct-horse-battery",
                    ),
                    follow_redirects=False,
                ),
                {303},
            )
        )
        web_harness.client.post(
            "/logout", data=web_harness.form(), follow_redirects=False
        )
    _assert_budget(registration_timings)

    login_timings: list[float] = []
    for _ in range(10):
        login_timings.append(
            _timed(
                lambda: web_harness.client.post(
                    "/login",
                    data=web_harness.form(
                        email="person-0@example.com",
                        password="correct-horse-battery",
                    ),
                    follow_redirects=False,
                ),
                {303},
            )
        )
        web_harness.client.post(
            "/logout", data=web_harness.form(), follow_redirects=False
        )
    _assert_budget(login_timings)


@pytest.mark.performance
def test_task_crud_and_htmx_status_handlers_stay_under_budget(
    web_harness: WebHarness,
) -> None:
    register(web_harness)
    web_harness.client.get("/tasks")

    create_timings = [
        _timed(
            lambda index=index: web_harness.client.post(
                "/tasks",
                data=web_harness.form(title=f"Task {index}"),
                headers={"HX-Request": "true"},
            ),
            {200},
        )
        for index in range(10)
    ]
    list_timings = [
        _timed(lambda: web_harness.client.get("/tasks"), {200}) for _ in range(10)
    ]
    edit_timings = [
        _timed(
            lambda index=index: web_harness.client.post(
                "/tasks/task0001/edit",
                data=web_harness.form(title=f"Edited {index}"),
                headers={"HX-Request": "true"},
            ),
            {200},
        )
        for index in range(10)
    ]
    toggle_timings = [
        _timed(
            lambda: web_harness.client.post(
                "/tasks/task0001/toggle",
                data=web_harness.form(),
                headers={"HX-Request": "true"},
            ),
            {200},
        )
        for _ in range(10)
    ]
    status_timings = [
        _timed(
            lambda: web_harness.client.get("/tasks/task0001/status"),
            {200},
        )
        for _ in range(10)
    ]
    delete_timings = [
        _timed(
            lambda task_id=task_id: web_harness.client.post(
                f"/tasks/task{task_id:04d}/delete",
                data=web_harness.form(),
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
