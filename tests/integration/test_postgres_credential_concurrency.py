from __future__ import annotations

import threading
from contextlib import ExitStack
from typing import TYPE_CHECKING, cast

import anyio
import pytest
from anyio.to_thread import run_sync as run_sync_in_worker_thread
from litestar.testing import TestClient
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from sqlalchemy import event, select

from app.adapters.persistence import (
    SqlAlchemyUnitOfWork,
    create_async_session_factory,
)
from app.adapters.persistence.models import UserModel
from app.adapters.security.clock import SystemClock
from app.adapters.security.passwords import PwdlibPasswordHasher
from app.application.auth import AuthResult, authenticate, register
from app.application.profiles import change_password
from app.domain.errors import InvalidCredentialsError
from app.main import create_app

if TYPE_CHECKING:
    from collections.abc import Callable

    from httpx import Response
    from litestar import Litestar
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.orm import Session

    from app.domain.users import User
    from tests.integration.conftest import DatabaseHarness

OLD_PASSWORD = "correct-horse-battery"
FIRST_NEW_PASSWORD = "first-replacement-password"
SECOND_NEW_PASSWORD = "second-replacement-password"


def _legacy_passwords() -> PwdlibPasswordHasher:
    return PwdlibPasswordHasher(
        PasswordHash(
            (
                Argon2Hasher(
                    time_cost=1,
                    memory_cost=8_192,
                    parallelism=1,
                ),
            )
        )
    )


def _copy_cookies(client: TestClient[Litestar]) -> dict[str, str]:
    return dict(client.cookies.items())


def _password_change_request(
    client: TestClient[Litestar],
    barrier: threading.Barrier,
    new_password: str,
) -> Response:
    barrier.wait(timeout=10)
    return client.post(
        "/profile/password",
        data={
            "_csrf_token": client.cookies["lsstack_csrf"],
            "current_password": OLD_PASSWORD,
            "new_password": new_password,
            "new_password_confirmation": new_password,
        },
        follow_redirects=False,
    )


def _for_update_signal(
    signal: threading.Event,
) -> Callable[[object, object, str, object, object, object], None]:
    def before_cursor_execute(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if "FOR UPDATE" in statement.upper():
            signal.set()

    return before_cursor_execute


@pytest.mark.integration
async def test_two_http_password_changes_serialize_on_the_user_row(  # noqa: PLR0915
    database_harness: DatabaseHarness,
) -> None:
    application = create_app(database_harness.settings)
    with (
        TestClient(
            app=application,
            session_config=application.state.session_config,
        ) as bootstrap,
        ExitStack() as clients,
    ):
        bootstrap.get("/register")
        registered = bootstrap.post(
            "/register",
            data={
                "_csrf_token": bootstrap.cookies["lsstack_csrf"],
                "email": "concurrent-change@example.com",
                "password": OLD_PASSWORD,
                "password_confirmation": OLD_PASSWORD,
            },
            follow_redirects=False,
        )
        assert registered.status_code == 303
        original_cookies = _copy_cookies(bootstrap)
        original_session_cookie = original_cookies["lsstack_session"]

        contender_apps = (
            create_app(database_harness.settings),
            create_app(database_harness.settings),
        )
        lock_attempts = (threading.Event(), threading.Event())
        for contender_app, lock_attempt in zip(
            contender_apps,
            lock_attempts,
            strict=True,
        ):
            contender_engine = cast("AsyncEngine", contender_app.state.engine)
            event.listen(
                contender_engine.sync_engine,
                "before_cursor_execute",
                _for_update_signal(lock_attempt),
            )
        contenders = tuple(
            clients.enter_context(
                TestClient(
                    app=contender_app,
                    session_config=contender_app.state.session_config,
                )
            )
            for contender_app in contender_apps
        )
        for contender in contenders:
            for name, value in original_cookies.items():
                contender.cookies.set(
                    name,
                    value,
                    domain="testserver.local",
                    path="/",
                )

        def user_id(session: Session) -> int:
            value = session.scalar(
                select(UserModel.id).where(
                    UserModel.email_normalized == "concurrent-change@example.com"
                )
            )
            assert value is not None
            return value

        persisted_user_id = await database_harness.runner.run(user_id)
        session_factory = create_async_session_factory(database_harness.engine)
        blocker = session_factory()
        responses: list[Response | None] = [None, None]
        barrier = threading.Barrier(2)

        async def invoke(index: int, new_password: str) -> None:
            responses[index] = await run_sync_in_worker_thread(
                _password_change_request,
                contenders[index],
                barrier,
                new_password,
            )

        try:
            await blocker.begin()
            await blocker.execute(
                select(UserModel)
                .where(UserModel.id == persisted_user_id)
                .with_for_update()
            )
            with anyio.fail_after(20):
                async with anyio.create_task_group() as task_group:
                    task_group.start_soon(invoke, 0, FIRST_NEW_PASSWORD)
                    task_group.start_soon(invoke, 1, SECOND_NEW_PASSWORD)
                    try:
                        for lock_attempt in lock_attempts:
                            assert await run_sync_in_worker_thread(
                                lock_attempt.wait,
                                10,
                            )
                        assert responses == [None, None]
                    finally:
                        # Release requests before task-group teardown on any failure.
                        await blocker.commit()
        finally:
            if blocker.in_transaction():
                await blocker.rollback()
            await blocker.close()

        completed = cast("tuple[Response, Response]", tuple(responses))
        assert sorted(response.status_code for response in completed) == [303, 422]
        winner_index = next(
            index
            for index, response in enumerate(completed)
            if response.status_code == 303
        )
        loser_index = 1 - winner_index
        winner_session_cookie = contenders[winner_index].cookies["lsstack_session"]
        loser_session_cookie = contenders[loser_index].cookies["lsstack_session"]
        assert winner_session_cookie != original_session_cookie
        assert loser_session_cookie != winner_session_cookie
        loser_session_headers = [
            value
            for value in completed[loser_index].headers.get_list("set-cookie")
            if value.startswith("lsstack_session=")
        ]
        assert loser_session_headers
        assert all(
            f"lsstack_session={winner_session_cookie}" not in value
            for value in loser_session_headers
        )

        # Only the successful request receives a usable version-1 session.
        assert contenders[winner_index].get("/profile").status_code == 200
        loser_profile = contenders[loser_index].get(
            "/profile",
            follow_redirects=False,
        )
        assert loser_profile.status_code == 303
        assert loser_profile.headers["location"] == "/login"

        old_session_app = create_app(database_harness.settings)
        old_session_client = clients.enter_context(
            TestClient(
                app=old_session_app,
                session_config=old_session_app.state.session_config,
            )
        )
        for name, value in original_cookies.items():
            old_session_client.cookies.set(
                name,
                value,
                domain="testserver.local",
                path="/",
            )
        revoked = old_session_client.get("/profile", follow_redirects=False)
        assert revoked.status_code == 303
        assert revoked.headers["location"] == "/login"

        def final_user(session: Session) -> User:
            user = SqlAlchemyUnitOfWork(session).users.get_by_id(persisted_user_id)
            assert user is not None
            return user

        persisted = await database_harness.runner.run(final_user)
        passwords = cast(
            "PwdlibPasswordHasher",
            application.state.web_dependencies.passwords,
        )
        expected_winner_password = (
            FIRST_NEW_PASSWORD if winner_index == 0 else SECOND_NEW_PASSWORD
        )
        expected_loser_password = (
            SECOND_NEW_PASSWORD if winner_index == 0 else FIRST_NEW_PASSWORD
        )
        assert persisted.session_version == 1
        assert passwords.verify(
            expected_winner_password,
            persisted.password_hash,
        ).valid
        assert not passwords.verify(
            expected_loser_password,
            persisted.password_hash,
        ).valid
        assert not passwords.verify(OLD_PASSWORD, persisted.password_hash).valid


async def _seed_obsolete_user(
    database_harness: DatabaseHarness,
    *,
    email: str,
) -> tuple[int, str]:
    passwords = _legacy_passwords()

    def operation(session: Session) -> tuple[int, str]:
        result = register(
            SqlAlchemyUnitOfWork(session),
            passwords,
            SystemClock(),
            email=email,
            password=OLD_PASSWORD,
        )
        user = SqlAlchemyUnitOfWork(session).users.get_by_id(result.user_id)
        assert user is not None
        return result.user_id, user.password_hash

    return await database_harness.runner.run(operation)


@pytest.mark.integration
@pytest.mark.parametrize("lookup", ["id", "email"])
async def test_locking_lookup_refreshes_retained_identity_map_state(
    database_harness: DatabaseHarness,
    lookup: str,
) -> None:
    email = f"retained-{lookup}@example.com"
    persisted_user_id, obsolete_hash = await _seed_obsolete_user(
        database_harness,
        email=email,
    )
    session_factory = create_async_session_factory(database_harness.engine)
    stale_session = session_factory()
    try:
        await stale_session.begin()
        retained_model = await stale_session.get(UserModel, persisted_user_id)
        assert retained_model is not None
        assert retained_model.password_hash == obsolete_hash

        def replace_credentials(session: Session) -> None:
            model = session.get(UserModel, persisted_user_id)
            assert model is not None
            model.password_hash = "concurrently-replaced-hash"
            model.session_version = 1
            session.flush()

        await database_harness.runner.run(replace_credentials)

        def locking_lookup(session: Session) -> User:
            users = SqlAlchemyUnitOfWork(session).users
            user = (
                users.get_by_id_for_update(persisted_user_id)
                if lookup == "id"
                else users.get_by_email_for_update(email)
            )
            assert user is not None
            return user

        locked_user = await stale_session.run_sync(locking_lookup)
        assert locked_user.password_hash == "concurrently-replaced-hash"
        assert locked_user.session_version == 1
        assert retained_model.password_hash == "concurrently-replaced-hash"
        assert retained_model.session_version == 1
    finally:
        if stale_session.in_transaction():
            await stale_session.rollback()
        await stale_session.close()


@pytest.mark.integration
@pytest.mark.parametrize("first_operation", ["hash-upgrade", "password-change"])
async def test_hash_upgrade_and_password_change_race_in_both_lock_orders(  # noqa: PLR0915
    database_harness: DatabaseHarness,
    first_operation: str,
) -> None:
    email = f"{first_operation}@example.com"
    persisted_user_id, obsolete_hash = await _seed_obsolete_user(
        database_harness,
        email=email,
    )
    passwords = PwdlibPasswordHasher()
    clock = SystemClock()
    session_factory = create_async_session_factory(database_harness.engine)
    first_session = session_factory()
    waiting_result: dict[str, object | BaseException] = {}
    waiting_completed = anyio.Event()
    lock_attempt = threading.Event()
    lock_listener = _for_update_signal(lock_attempt)
    listener_attached = False

    def login(session: Session) -> AuthResult:
        return authenticate(
            SqlAlchemyUnitOfWork(session),
            passwords,
            clock,
            email=email,
            password=OLD_PASSWORD,
        )

    def password_change(session: Session) -> int:
        return change_password(
            SqlAlchemyUnitOfWork(session),
            passwords,
            clock,
            user_id=persisted_user_id,
            current_password=OLD_PASSWORD,
            new_password=FIRST_NEW_PASSWORD,
            new_password_confirmation=FIRST_NEW_PASSWORD,
        )

    async def run_waiter() -> None:
        operation = password_change if first_operation == "hash-upgrade" else login
        try:
            waiting_result["value"] = await database_harness.runner.run(operation)
        except BaseException as error:
            # The password-change-first case deliberately expects this domain error.
            waiting_result["error"] = error
        finally:
            waiting_completed.set()

    try:
        await first_session.begin()
        if first_operation == "hash-upgrade":
            first_result = await first_session.run_sync(login)
            assert first_result.session_version == 0
        else:
            assert await first_session.run_sync(password_change) == 1

        event.listen(
            database_harness.engine.sync_engine,
            "before_cursor_execute",
            lock_listener,
        )
        listener_attached = True
        with anyio.fail_after(20):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(run_waiter)
                try:
                    assert await run_sync_in_worker_thread(lock_attempt.wait, 10)
                    assert not waiting_completed.is_set()
                finally:
                    event.remove(
                        database_harness.engine.sync_engine,
                        "before_cursor_execute",
                        lock_listener,
                    )
                    listener_attached = False
                    await first_session.commit()
    finally:
        if listener_attached:
            event.remove(
                database_harness.engine.sync_engine,
                "before_cursor_execute",
                lock_listener,
            )
        if first_session.in_transaction():
            await first_session.rollback()
        await first_session.close()

    if first_operation == "hash-upgrade":
        assert waiting_result == {"value": 1}
    else:
        error = waiting_result.get("error")
        assert isinstance(error, InvalidCredentialsError)
        assert "value" not in waiting_result

    def final_user(session: Session) -> User:
        user = SqlAlchemyUnitOfWork(session).users.get_by_id(persisted_user_id)
        assert user is not None
        return user

    persisted = await database_harness.runner.run(final_user)
    assert persisted.password_hash != obsolete_hash
    assert persisted.session_version == 1
    assert passwords.verify(
        FIRST_NEW_PASSWORD,
        persisted.password_hash,
    ).valid
    assert not passwords.verify(OLD_PASSWORD, persisted.password_hash).valid


@pytest.mark.integration
async def test_uncontended_obsolete_hash_login_upgrades_without_revocation(
    database_harness: DatabaseHarness,
) -> None:
    email = "uncontended-upgrade@example.com"
    persisted_user_id, obsolete_hash = await _seed_obsolete_user(
        database_harness,
        email=email,
    )
    passwords = PwdlibPasswordHasher()

    def login(session: Session) -> AuthResult:
        return authenticate(
            SqlAlchemyUnitOfWork(session),
            passwords,
            SystemClock(),
            email=email,
            password=OLD_PASSWORD,
        )

    result = await database_harness.runner.run(login)
    assert result.session_version == 0

    def final_user(session: Session) -> User:
        user = SqlAlchemyUnitOfWork(session).users.get_by_id(persisted_user_id)
        assert user is not None
        return user

    persisted = await database_harness.runner.run(final_user)
    verification = passwords.verify(OLD_PASSWORD, persisted.password_hash)
    assert persisted.password_hash != obsolete_hash
    assert persisted.session_version == 0
    assert verification.valid
    assert verification.replacement_hash is None
