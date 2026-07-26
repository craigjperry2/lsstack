from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from litestar.testing import TestClient

from app.main import create_app

if TYPE_CHECKING:
    from httpx import Response
    from litestar import Litestar

    from tests.integration.conftest import DatabaseHarness


def _form(client: TestClient[Litestar], **values: str) -> dict[str, str]:
    return {"_csrf_token": client.cookies["lsstack_csrf"], **values}


def _register(
    client: TestClient[Litestar],
    *,
    email: str,
    password: str,
) -> Response:
    client.get("/register")
    return client.post(
        "/register",
        data=_form(
            client,
            email=email,
            password=password,
            password_confirmation=password,
        ),
        follow_redirects=False,
    )


def _login(client: TestClient[Litestar], *, email: str, password: str) -> Response:
    client.get("/login")
    return client.post(
        "/login",
        data=_form(client, email=email, password=password),
        follow_redirects=False,
    )


@pytest.mark.integration
async def test_real_postgres_web_auth_session_ownership_and_task_crud(
    database_harness: DatabaseHarness,
) -> None:
    application = create_app(database_harness.settings)
    public_ids = application.state.web_dependencies.public_ids

    with TestClient(
        app=application,
        session_config=application.state.session_config,
    ) as client:
        assert (
            _register(
                client,
                email="alice@example.com",
                password="correct-horse-battery",
            ).status_code
            == 303
        )
        created = client.post(
            "/tasks",
            data=_form(client, title="Alice private task", description="Private"),
            follow_redirects=False,
        )
        assert created.status_code == 303
        alice_task = public_ids.encode(1)

        assert (
            client.post(
                "/logout", data=_form(client), follow_redirects=False
            ).status_code
            == 303
        )
        assert (
            _register(
                client,
                email="bob@example.com",
                password="correct-horse-battery",
            ).status_code
            == 303
        )
        assert client.get(f"/tasks/{alice_task}/edit").status_code == 404

        client.post("/logout", data=_form(client), follow_redirects=False)
        assert (
            _login(
                client,
                email="alice@example.com",
                password="incorrect-password",
            ).status_code
            == 401
        )
        assert (
            _login(
                client,
                email="alice@example.com",
                password="correct-horse-battery",
            ).status_code
            == 303
        )
        old_cookie = client.cookies["lsstack_session"]

        assert client.get(f"/tasks/{alice_task}/edit").status_code == 200
        assert (
            client.post(
                f"/tasks/{alice_task}/edit",
                data=_form(client, title="Revised task", description=""),
                follow_redirects=False,
            ).status_code
            == 303
        )
        toggled = client.post(
            f"/tasks/{alice_task}/toggle",
            data=_form(client),
            headers={"HX-Request": "true"},
        )
        assert toggled.status_code == 200
        assert "Mark incomplete" in toggled.text
        assert "Processing" in client.get(f"/tasks/{alice_task}/status").text

        changed = client.post(
            "/profile/password",
            data=_form(
                client,
                current_password="correct-horse-battery",
                new_password="replacement-password",
                new_password_confirmation="replacement-password",
            ),
            follow_redirects=False,
        )
        assert changed.status_code == 303
        assert client.cookies["lsstack_session"] != old_cookie

        client.cookies.clear(domain="testserver.local", path="/")
        client.cookies.set(
            "lsstack_session",
            old_cookie,
            domain="testserver.local",
            path="/",
        )
        revoked = client.get("/profile", follow_redirects=False)
        assert revoked.status_code == 303
        assert revoked.headers["location"] == "/login"

        assert (
            _login(
                client,
                email="alice@example.com",
                password="correct-horse-battery",
            ).status_code
            == 401
        )
        assert (
            _login(
                client,
                email="alice@example.com",
                password="replacement-password",
            ).status_code
            == 303
        )
        assert (
            client.post(
                f"/tasks/{alice_task}/delete",
                data=_form(client),
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert "Revised task" not in client.get("/tasks").text

        assert (
            client.post(
                "/logout", data=_form(client), follow_redirects=False
            ).status_code
            == 303
        )
        assert client.get("/profile", follow_redirects=False).status_code == 303
