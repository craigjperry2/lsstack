from __future__ import annotations

from statistics import median
from time import perf_counter
from typing import TYPE_CHECKING

import pytest
from litestar.testing import TestClient
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from sqlalchemy import select

from app.adapters.persistence.models import UserModel
from app.adapters.security.passwords import PwdlibPasswordHasher
from app.main import create_app

if TYPE_CHECKING:
    from collections.abc import Callable

    from httpx import Response
    from litestar import Litestar
    from sqlalchemy.orm import Session

    from tests.integration.conftest import DatabaseHarness

PASSWORD = "correct-horse-battery"
SAMPLE_COUNT = 10


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
    assert isinstance(dependencies.passwords, PwdlibPasswordHasher)
    public_ids = dependencies.public_ids
    legacy_password_hash = PasswordHash(
        (
            Argon2Hasher(
                time_cost=1,
                memory_cost=1_024,
                parallelism=1,
            ),
        )
    )
    legacy_hashes = {
        f"legacy-perf-{index}@example.com": legacy_password_hash.hash(PASSWORD)
        for index in range(SAMPLE_COUNT)
    }
    assert len(set(legacy_hashes.values())) == SAMPLE_COUNT

    def seed_legacy_users(session: Session) -> None:
        session.add_all(
            UserModel(
                email_normalized=email,
                password_hash=password_hash,
                session_version=0,
            )
            for email, password_hash in legacy_hashes.items()
        )

    await database_harness.runner.run(seed_legacy_users)

    with TestClient(
        app=application,
        session_config=application.state.session_config,
    ) as client:
        # Client startup, CSRF acquisition, and a complete Argon2 register/login
        # warm-up are intentionally outside every measured sample.
        client.get("/register")
        client.post(
            "/register",
            data=_form(
                client,
                email="warmup@example.com",
                password=PASSWORD,
                password_confirmation=PASSWORD,
            ),
            follow_redirects=False,
        )
        client.post("/logout", data=_form(client), follow_redirects=False)
        client.post(
            "/login",
            data=_form(
                client,
                email="warmup@example.com",
                password=PASSWORD,
            ),
            follow_redirects=False,
        )
        client.post("/logout", data=_form(client), follow_redirects=False)

        registration_timings: list[float] = []
        for index in range(SAMPLE_COUNT):
            registration_timings.append(
                _timed(
                    lambda index=index: client.post(
                        "/register",
                        data=_form(
                            client,
                            email=f"perf-{index}@example.com",
                            password=PASSWORD,
                            password_confirmation=PASSWORD,
                        ),
                        follow_redirects=False,
                    ),
                    {303},
                )
            )
            client.post("/logout", data=_form(client), follow_redirects=False)
        _assert_budget(registration_timings)

        login_timings: list[float] = []
        for _ in range(SAMPLE_COUNT):
            login_timings.append(
                _timed(
                    lambda: client.post(
                        "/login",
                        data=_form(
                            client,
                            email="perf-0@example.com",
                            password=PASSWORD,
                        ),
                        follow_redirects=False,
                    ),
                    {303},
                )
            )
            client.post("/logout", data=_form(client), follow_redirects=False)
        _assert_budget(login_timings)

        upgrade_login_timings: list[float] = []
        for email in legacy_hashes:
            upgrade_login_timings.append(
                _timed(
                    lambda email=email: client.post(
                        "/login",
                        data=_form(
                            client,
                            email=email,
                            password=PASSWORD,
                        ),
                        follow_redirects=False,
                    ),
                    {303},
                )
            )
            client.post("/logout", data=_form(client), follow_redirects=False)
        _assert_budget(upgrade_login_timings)

        client.post(
            "/login",
            data=_form(
                client,
                email="perf-0@example.com",
                password=PASSWORD,
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

    def upgraded_hashes(session: Session) -> dict[str, str]:
        rows = session.execute(
            select(UserModel.email_normalized, UserModel.password_hash).where(
                UserModel.email_normalized.in_(legacy_hashes)
            )
        )
        return {str(email): str(password_hash) for email, password_hash in rows}

    persisted_hashes = await database_harness.runner.run(upgraded_hashes)
    assert persisted_hashes.keys() == legacy_hashes.keys()
    for email, password_hash in persisted_hashes.items():
        assert password_hash != legacy_hashes[email]
        verification = dependencies.passwords.verify(PASSWORD, password_hash)
        assert verification.valid is True
        assert verification.replacement_hash is None

    for timings in (
        create_timings,
        list_timings,
        edit_timings,
        toggle_timings,
        status_timings,
        delete_timings,
    ):
        _assert_budget(timings)
