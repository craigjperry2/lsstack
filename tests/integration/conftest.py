from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from saq.queue.postgres import PostgresQueue
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.adapters.persistence import TransactionRunner, create_async_session_factory
from app.adapters.persistence.engine import create_engine
from app.config import Settings, load_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

SAFE_DATABASE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*_test$")


@dataclass(frozen=True, slots=True)
class DatabaseHarness:
    settings: Settings
    engine: AsyncEngine
    runner: TransactionRunner
    alembic: Config
    database_name: str


def _database_url(value: str | URL, database: str) -> URL:
    return make_url(value).set(database=database)


def _require_safe_target(settings: Settings) -> tuple[str, URL, URL]:
    target = make_url(settings.test_database_url)
    admin = make_url(settings.admin_database_url)
    database_name = target.database or ""
    if not SAFE_DATABASE_NAME.fullmatch(database_name):
        pytest.fail(
            "Refusing database integration setup: TEST_DATABASE_URL database "
            "must match ^[A-Za-z][A-Za-z0-9_]*_test$."
        )
    if admin.database == database_name:
        pytest.fail(
            "Refusing database integration setup: ADMIN_DATABASE_URL must connect "
            "to a different maintenance database."
        )
    if target.get_backend_name() != "postgresql" or admin.get_backend_name() != (
        "postgresql"
    ):
        pytest.fail("Database integration tests require PostgreSQL URLs.")
    return database_name, target, admin


async def _admin_statement(engine: AsyncEngine, statement: str) -> None:
    async with engine.connect() as connection:
        await connection.execute(text(statement))


async def _terminate_connections(engine: AsyncEngine, database_name: str) -> None:
    async with engine.connect() as connection:
        await connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :database_name AND pid <> pg_backend_pid()"
            ),
            {"database_name": database_name},
        )


async def _initialize_saq(url: str) -> None:
    queue = PostgresQueue.from_url(url, min_size=1, max_size=2)
    try:
        await queue.connect()
    finally:
        await queue.disconnect()


@pytest_asyncio.fixture
async def database_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[DatabaseHarness]:
    if os.environ.get("RUN_DATABASE_TESTS") != "1":
        pytest.skip(
            "Set RUN_DATABASE_TESTS=1 to enable destructive disposable DB tests."
        )
    settings = load_settings(env_file=None)
    database_name, target_url, admin_url = _require_safe_target(settings)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    quoted_database = f'"{database_name}"'
    try:
        await _terminate_connections(admin_engine, database_name)
        await _admin_statement(
            admin_engine, f"DROP DATABASE IF EXISTS {quoted_database}"
        )
        await _admin_statement(
            admin_engine, f"CREATE DATABASE {quoted_database} OWNER app_user"
        )
    finally:
        await admin_engine.dispose()

    target_admin_url = _database_url(admin_url, database_name)
    target_admin_engine = create_async_engine(target_admin_url)
    try:
        async with target_admin_engine.begin() as connection:
            await connection.execute(text("REVOKE ALL ON SCHEMA public FROM PUBLIC"))
            await connection.execute(text("CREATE SCHEMA app AUTHORIZATION app_user"))
            await connection.execute(text("CREATE SCHEMA saq AUTHORIZATION saq_user"))
            await connection.execute(
                text("REVOKE ALL ON SCHEMA app FROM PUBLIC, saq_user")
            )
            await connection.execute(
                text("REVOKE ALL ON SCHEMA saq FROM PUBLIC, app_user")
            )
            await connection.execute(
                text(
                    f"GRANT CONNECT ON DATABASE {quoted_database} TO app_user, saq_user"
                )
            )
            await connection.execute(
                text(
                    "ALTER ROLE app_user IN DATABASE "
                    f"{quoted_database} SET search_path TO pg_catalog, app"
                )
            )
            await connection.execute(
                text(
                    "ALTER ROLE saq_user IN DATABASE "
                    f"{quoted_database} SET search_path TO saq, pg_catalog"
                )
            )
    finally:
        await target_admin_engine.dispose()

    target_url_string = target_url.render_as_string(hide_password=False)
    saq_target_url = _database_url(
        settings.saq_database_url, database_name
    ).render_as_string(hide_password=False)
    await _initialize_saq(saq_target_url)

    monkeypatch.setenv("DATABASE_URL", target_url_string)
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", target_url_string)
    command.upgrade(alembic_config, "head")
    engine = create_engine(target_url_string)
    runner = TransactionRunner(create_async_session_factory(engine))
    try:
        yield DatabaseHarness(
            settings=settings.model_copy(
                update={
                    "database_url": target_url_string,
                    "test_database_url": target_url_string,
                    "saq_database_url": saq_target_url,
                    "environment": "test",
                    "debug": False,
                    "telemetry_enabled": False,
                    "trusted_hosts": (
                        "testserver.local",
                        "localhost",
                        "127.0.0.1",
                    ),
                }
            ),
            engine=engine,
            runner=runner,
            alembic=alembic_config,
            database_name=database_name,
        )
    finally:
        await engine.dispose()
        cleanup_admin_engine = create_async_engine(
            admin_url, isolation_level="AUTOCOMMIT"
        )
        try:
            await _terminate_connections(cleanup_admin_engine, database_name)
            await _admin_statement(
                cleanup_admin_engine, f"DROP DATABASE IF EXISTS {quoted_database}"
            )
        finally:
            await cleanup_admin_engine.dispose()
