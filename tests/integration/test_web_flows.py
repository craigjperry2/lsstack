from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

from app.adapters.web import middleware as middleware_module
from app.application.tasks import process_task_created
from tests.conftest import FakeTransactionRunner

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
    from httpx import Response

    from tests.conftest import WebHarness


def register(harness: WebHarness) -> None:
    assert harness.client.get("/register").status_code == 200
    response = harness.client.post(
        "/register",
        data=harness.form(
            email="Person@Example.com",
            password="correct-horse-battery",
            password_confirmation="correct-horse-battery",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/tasks"


def test_root_registration_auto_login_and_logout(web_harness: WebHarness) -> None:
    response = web_harness.client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    register(web_harness)
    assert web_harness.client.get("/tasks").status_code == 200
    response = web_harness.client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/tasks"

    response = web_harness.client.post(
        "/logout",
        data=web_harness.form(),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert (
        web_harness.client.get("/profile", follow_redirects=False).headers["location"]
        == "/login"
    )


def test_login_is_generic_and_duplicate_registration_is_atomic(
    web_harness: WebHarness,
) -> None:
    register(web_harness)
    web_harness.client.post("/logout", data=web_harness.form(), follow_redirects=False)
    web_harness.client.get("/login")
    for email in ("missing@example.com", "not-an-email"):
        response = web_harness.client.post(
            "/login",
            data=web_harness.form(email=email, password="incorrect-password"),
        )
        assert response.status_code == 401
        assert "Invalid email or password." in response.text

    web_harness.client.get("/register")
    response = web_harness.client.post(
        "/register",
        data=web_harness.form(
            email="person@example.com",
            password="another-valid-password",
            password_confirmation="another-valid-password",
        ),
    )
    assert response.status_code == 409
    assert len(web_harness.uow.users.values) == 1


def test_csrf_rejects_missing_malformed_and_mismatched_tokens(
    web_harness: WebHarness,
) -> None:
    web_harness.client.get("/register")
    valid_data = {
        "email": "person@example.com",
        "password": "correct-horse-battery",
        "password_confirmation": "correct-horse-battery",
    }
    for data in (
        valid_data,
        {"_csrf_token": "malformed", **valid_data},
        {"_csrf_token": web_harness.csrf() + "x", **valid_data},
    ):
        response = web_harness.client.post("/register", data=data)
        assert response.status_code == 403

    register(web_harness)
    unsafe_routes: tuple[tuple[str, dict[str, str]], ...] = (
        ("/logout", {}),
        (
            "/profile/password",
            {
                "current_password": "correct-horse-battery",
                "new_password": "replacement-password",
                "new_password_confirmation": "replacement-password",
            },
        ),
        ("/tasks", {"title": "Task"}),
        ("/tasks/not-a-public-id/toggle", {}),
        ("/tasks/not-a-public-id/delete", {}),
        ("/tasks/not-a-public-id/edit", {"title": "Task"}),
    )
    for path, data in unsafe_routes:
        response = web_harness.client.post(
            path, data=data, headers={"HX-Request": "true"}
        )
        assert response.status_code == 403


def test_fixed_expiry_is_enforced_without_sliding_renewal(
    web_harness: WebHarness,
) -> None:
    register(web_harness)
    set_cookie = web_harness.client.get("/tasks").headers.get("set-cookie", "")
    if set_cookie:
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "Secure" not in set_cookie
    web_harness.clock.value += timedelta(hours=11)
    assert web_harness.client.get("/tasks").status_code == 200

    # The encrypted cookie is re-serialized with a fresh nonce on each response,
    # but the embedded application expiry remains the original absolute deadline.
    web_harness.clock.value += timedelta(hours=1, seconds=1)
    response = web_harness.client.get("/tasks", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_tampered_encrypted_cookie_is_rejected(web_harness: WebHarness) -> None:
    register(web_harness)
    encrypted = web_harness.client.cookies["lsstack_session"]
    middle = len(encrypted) // 2
    replacement = "A" if encrypted[middle] != "A" else "B"
    tampered = f"{encrypted[:middle]}{replacement}{encrypted[middle + 1 :]}"
    web_harness.client.cookies.clear(domain="testserver.local", path="/")
    web_harness.client.cookies.set(
        "lsstack_session",
        tampered,
        domain="testserver.local",
        path="/",
    )
    response = web_harness.client.get("/tasks", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_password_change_revokes_old_cookie_and_reissues_current_browser(
    web_harness: WebHarness,
) -> None:
    register(web_harness)
    old_cookie = web_harness.client.cookies["lsstack_session"]
    assert web_harness.client.get("/profile").status_code == 200

    wrong = web_harness.client.post(
        "/profile/password",
        data=web_harness.form(
            current_password="wrong-password",
            new_password="replacement-password",
            new_password_confirmation="replacement-password",
        ),
    )
    assert wrong.status_code == 422
    assert "Current password is incorrect." in wrong.text

    changed = web_harness.client.post(
        "/profile/password",
        data=web_harness.form(
            current_password="correct-horse-battery",
            new_password="replacement-password",
            new_password_confirmation="replacement-password",
        ),
        follow_redirects=False,
    )
    assert changed.status_code == 303
    new_cookie = web_harness.client.cookies["lsstack_session"]
    assert new_cookie != old_cookie
    assert web_harness.client.get("/profile").status_code == 200

    web_harness.client.cookies.set("lsstack_session", old_cookie)
    response = web_harness.client.get("/profile", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_full_page_and_htmx_task_crud_and_pending_transition(
    web_harness: WebHarness,
) -> None:
    register(web_harness)
    created = web_harness.client.post(
        "/tasks",
        data=web_harness.form(title="First task", description="Details"),
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert len(web_harness.uow.tasks.values) == 1
    assert len(web_harness.uow.outbox.values) == 1

    page = web_harness.client.get("/tasks")
    assert "First task" in page.text
    assert 'hx-trigger="every 400ms"' in page.text
    public_id = "task0001"

    toggled = web_harness.client.post(
        f"/tasks/{public_id}/toggle",
        data=web_harness.form(),
        headers={"HX-Request": "true"},
    )
    assert toggled.status_code == 200
    assert "Mark incomplete" in toggled.text

    edit = web_harness.client.get(f"/tasks/{public_id}/edit")
    assert edit.status_code == 200
    updated = web_harness.client.post(
        f"/tasks/{public_id}/edit",
        data=web_harness.form(title="Revised", description=""),
        follow_redirects=False,
    )
    assert updated.status_code == 303

    pending = web_harness.client.get(f"/tasks/{public_id}/status")
    assert "Processing" in pending.text
    process_task_created(web_harness.uow, web_harness.clock, task_id=1)
    processed = web_harness.client.get(f"/tasks/{public_id}/status")
    assert "Processed" in processed.text
    assert "hx-trigger" not in processed.text

    deleted = web_harness.client.post(
        f"/tasks/{public_id}/delete",
        data=web_harness.form(),
        headers={"HX-Request": "true"},
    )
    assert deleted.status_code == 200
    assert deleted.content == b""
    assert web_harness.uow.tasks.values == {}


def test_authenticated_handlers_use_one_transaction_bridge_call(
    web_harness: WebHarness,
) -> None:
    register(web_harness)

    def assert_one_call(action: Callable[[], Response]) -> None:
        web_harness.transactions.run_calls = 0
        response = action()
        assert response.status_code < 400
        assert web_harness.transactions.run_calls == 1

    assert_one_call(
        lambda: web_harness.client.get("/profile"),
    )
    assert_one_call(
        lambda: web_harness.client.get("/tasks"),
    )
    assert_one_call(
        lambda: web_harness.client.post(
            "/tasks",
            data=web_harness.form(title="One transaction"),
            follow_redirects=False,
        ),
    )
    assert_one_call(
        lambda: web_harness.client.get("/tasks/task0001/edit"),
    )
    assert_one_call(
        lambda: web_harness.client.post(
            "/tasks/task0001/toggle",
            data=web_harness.form(),
            headers={"HX-Request": "true"},
        ),
    )
    assert_one_call(
        lambda: web_harness.client.get("/tasks/task0001/status"),
    )
    assert_one_call(
        lambda: web_harness.client.post(
            "/logout",
            data=web_harness.form(),
            follow_redirects=False,
        ),
    )


def test_invalid_htmx_create_does_not_repeat_existing_rows(
    web_harness: WebHarness,
) -> None:
    register(web_harness)
    created = web_harness.client.post(
        "/tasks",
        data=web_harness.form(title="Existing task"),
        headers={"HX-Request": "true"},
    )
    assert created.status_code == 200
    assert 'hx-swap-oob="afterbegin:#task-list"' in created.text

    invalid = web_harness.client.post(
        "/tasks",
        data=web_harness.form(title=" "),
        headers={"HX-Request": "true"},
    )
    assert invalid.status_code == 422
    assert "Title is required." in invalid.text
    assert "Existing task" not in invalid.text
    assert "task-row" not in invalid.text
    assert len(web_harness.uow.tasks.values) == 1


def test_invalid_and_unowned_public_ids_are_not_found(
    web_harness: WebHarness,
) -> None:
    register(web_harness)
    web_harness.client.post("/tasks", data=web_harness.form(title="Private task"))
    web_harness.uow.tasks.values[1] = replace(
        web_harness.uow.tasks.values[1], user_id=999
    )
    for public_id in ("invalid", "task0001"):
        assert web_harness.client.get(f"/tasks/{public_id}/edit").status_code == 404


def test_security_headers_and_request_id(web_harness: WebHarness) -> None:
    response = web_harness.client.get(
        "/login", headers={"X-Request-ID": "safe-request_123"}
    )
    assert response.headers["x-request-id"] == "safe-request_123"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]

    generated = web_harness.client.get(
        "/login", headers={"X-Request-ID": "unsafe request id"}
    ).headers["x-request-id"]
    assert len(generated) == 32


def test_request_completion_log_is_structured_and_query_free(
    web_harness: WebHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    class RecordingLogger:
        def info(self, event: str, **fields: object) -> None:
            events.append((event, fields))

    monkeypatch.setattr(middleware_module, "LOGGER", RecordingLogger())
    response = web_harness.client.get(
        "/livez?private=do-not-log",
        headers={"X-Request-ID": "request-log-test"},
    )

    assert response.status_code == 200
    event, fields = events[-1]
    assert event == "request.complete"
    assert fields["request_id"] == "request-log-test"
    assert fields["method"] == "GET"
    assert fields["path"] == "/livez"
    assert fields["status"] == 200
    assert isinstance(fields["duration_ms"], float)
    assert "private" not in repr(fields)


def test_liveness_and_readiness_have_distinct_semantics(
    web_harness: WebHarness,
) -> None:
    assert web_harness.client.get("/livez").json() == {"status": "ok"}
    assert web_harness.client.get("/readyz").json() == {"status": "ok"}

    class UnreadyTransactionRunner(FakeTransactionRunner):
        async def probe(self) -> bool:
            return False

    dependencies = web_harness.app.state.web_dependencies
    web_harness.app.state.web_dependencies = replace(
        dependencies,
        transactions=UnreadyTransactionRunner(web_harness.uow),
    )
    response = web_harness.client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    # Liveness remains independent of database readiness.
    assert web_harness.client.get("/livez").status_code == 200
