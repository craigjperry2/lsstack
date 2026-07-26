from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from app.application.auth import authenticate, register
from app.application.outbox import claim_messages, fail_message
from app.application.ports.security import PasswordVerification
from app.application.profiles import change_password
from app.application.tasks import (
    create_task,
    delete_task,
    list_tasks,
    process_task_created,
    toggle_task,
    update_task,
)
from app.domain.errors import (
    CurrentPasswordMismatchError,
    InvalidCredentialsError,
    TaskNotFoundError,
    ValidationError,
)
from app.domain.tasks import Task
from app.domain.users import normalize_email, validate_password
from tests.conftest import FakeClock, FakePasswordHasher, FakeUnitOfWork


def test_email_and_password_policy() -> None:
    assert normalize_email("  USER@Example.COM ") == "user@example.com"
    with pytest.raises(ValidationError):
        normalize_email("not-an-email")
    for bad_password in (
        "too-short",
        " leading-whitespace",
        "trailing-whitespace ",
        "prefix-user@example.com-suffix",
    ):
        with pytest.raises(ValidationError):
            validate_password(bad_password, "user@example.com")


def test_immutable_domain_value() -> None:
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    task = Task(
        id=None,
        user_id=1,
        title="Title",
        description=None,
        is_completed=False,
        background_processed_at=None,
        created_at=instant,
        updated_at=instant,
    )
    with pytest.raises(FrozenInstanceError):
        task.title = "Changed"  # type: ignore[misc]


def test_authentication_profile_and_task_lifecycle() -> None:
    uow = FakeUnitOfWork()
    clock = FakeClock()
    passwords = FakePasswordHasher()
    auth = register(
        uow,
        passwords,
        clock,
        email="USER@example.com",
        password="correct-horse-battery",
    )
    assert auth.email_normalized == "user@example.com"
    assert (
        authenticate(
            uow,
            passwords,
            clock,
            email="user@example.com",
            password="correct-horse-battery",
        ).user_id
        == auth.user_id
    )
    with pytest.raises(InvalidCredentialsError):
        authenticate(
            uow,
            passwords,
            clock,
            email="missing@example.com",
            password="correct-horse-battery",
        )
    with pytest.raises(CurrentPasswordMismatchError):
        change_password(
            uow,
            passwords,
            clock,
            user_id=auth.user_id,
            current_password="wrong-password",
            new_password="replacement-password",
            new_password_confirmation="replacement-password",
        )
    new_version = change_password(
        uow,
        passwords,
        clock,
        user_id=auth.user_id,
        current_password="correct-horse-battery",
        new_password="replacement-password",
        new_password_confirmation="replacement-password",
    )
    assert new_version == 1

    created = create_task(
        uow,
        clock,
        user_id=auth.user_id,
        title="  First task ",
        description=" Details ",
    )
    assert created.task.title == "First task"
    assert created.task.description == "Details"
    assert len(uow.outbox.values) == 1
    assert list_tasks(uow, user_id=auth.user_id) == (created.task,)
    toggled = toggle_task(uow, clock, user_id=auth.user_id, task_id=created.task.id)
    assert toggled.is_completed
    updated = update_task(
        uow,
        clock,
        user_id=auth.user_id,
        task_id=created.task.id,
        title="Revised",
        description=None,
    )
    assert updated.title == "Revised"
    first = process_task_created(uow, clock, task_id=created.task.id)
    clock.value += timedelta(seconds=1)
    duplicate = process_task_created(uow, clock, task_id=created.task.id)
    assert first is not None
    assert duplicate is not None
    assert first.background_processed_at == duplicate.background_processed_at
    delete_task(uow, user_id=auth.user_id, task_id=created.task.id)
    with pytest.raises(TaskNotFoundError):
        delete_task(uow, user_id=auth.user_id, task_id=created.task.id)


def test_task_ownership_is_indistinguishable_from_absence() -> None:
    uow = FakeUnitOfWork()
    clock = FakeClock()
    created = create_task(uow, clock, user_id=1, title="Private")
    with pytest.raises(TaskNotFoundError):
        update_task(
            uow,
            clock,
            user_id=2,
            task_id=created.task.id,
            title="Stolen",
            description=None,
        )


def test_successful_login_upgrades_an_obsolete_hash() -> None:
    uow = FakeUnitOfWork()
    clock = FakeClock()
    original = register(
        uow,
        FakePasswordHasher(),
        clock,
        email="upgrade@example.com",
        password="correct-horse-battery",
    )
    user = uow.users.values[original.user_id]
    uow.users.values[original.user_id] = user.__class__(
        id=user.id,
        email_normalized=user.email_normalized,
        password_hash="legacy-hash",
        session_version=user.session_version,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )

    class UpgradingHasher(FakePasswordHasher):
        def verify(self, password: str, password_hash: str) -> PasswordVerification:
            assert password_hash == "legacy-hash"
            return PasswordVerification(
                valid=password == "correct-horse-battery",
                replacement_hash="current-hash",
            )

    authenticate(
        uow,
        UpgradingHasher(),
        clock,
        email="upgrade@example.com",
        password="correct-horse-battery",
    )
    assert uow.users.values[original.user_id].password_hash == "current-hash"
    assert uow.users.calls == [
        "get_by_email_for_update",
        "update_credentials",
    ]


def test_credential_use_cases_select_locking_repository_reads() -> None:
    uow = FakeUnitOfWork()
    clock = FakeClock()
    passwords = FakePasswordHasher()
    auth = register(
        uow,
        passwords,
        clock,
        email="locking@example.com",
        password="correct-horse-battery",
    )

    authenticate(
        uow,
        passwords,
        clock,
        email="locking@example.com",
        password="correct-horse-battery",
    )
    assert uow.users.calls == ["get_by_email_for_update"]

    uow.users.calls.clear()
    assert (
        change_password(
            uow,
            passwords,
            clock,
            user_id=auth.user_id,
            current_password="correct-horse-battery",
            new_password="replacement-password",
            new_password_confirmation="replacement-password",
        )
        == 1
    )
    assert uow.users.calls == [
        "get_by_id_for_update",
        "update_credentials",
    ]


def test_fake_credential_update_preserves_persisted_identity_fields() -> None:
    uow = FakeUnitOfWork()
    auth = register(
        uow,
        FakePasswordHasher(),
        FakeClock(),
        email="identity@example.com",
        password="correct-horse-battery",
    )
    persisted = uow.users.values[auth.user_id]

    updated = uow.users.update_credentials(
        replace(
            persisted,
            email_normalized="stale@example.com",
            password_hash="replacement-hash",
            session_version=1,
        )
    )

    assert updated.email_normalized == "identity@example.com"
    assert updated.created_at == persisted.created_at
    assert updated.password_hash == "replacement-hash"
    assert updated.session_version == 1


def test_failed_outbox_publication_retries_only_after_lease_expiry() -> None:
    uow = FakeUnitOfWork()
    clock = FakeClock()
    created = create_task(uow, clock, user_id=1, title="Queued")
    claimed = claim_messages(
        uow,
        clock,
        owner="worker-a",
        lease_duration=timedelta(seconds=30),
    )
    assert len(claimed) == 1
    assert fail_message(
        uow,
        message_id=created.outbox_message_id,
        owner="worker-a",
        error="queue down",
    )
    assert (
        claim_messages(
            uow,
            clock,
            owner="worker-b",
            lease_duration=timedelta(seconds=30),
        )
        == ()
    )
    clock.value += timedelta(seconds=31)
    retried = claim_messages(
        uow,
        clock,
        owner="worker-b",
        lease_duration=timedelta(seconds=30),
    )
    assert len(retried) == 1
    assert retried[0].attempt_count == 2
