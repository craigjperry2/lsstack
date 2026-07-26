"""Litestar composition root.

Importing this module creates the CLI-discoverable ``app`` object.  Settings are
loaded here, never by the domain or application layers.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from litestar import Litestar
from litestar.config.allowed_hosts import AllowedHostsConfig
from litestar.config.compression import CompressionConfig
from litestar.config.csrf import CSRFConfig
from litestar.datastructures import CacheControlHeader, State
from litestar.middleware.session.client_side import CookieBackendConfig
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.static_files import (
    create_static_files_router,  # pyright: ignore[reportUnknownVariableType]
)
from litestar.template.config import TemplateConfig

from app.adapters.observability import (
    TelemetryRuntime,
    configure_logging,
    event_loop_lag_monitor,
)
from app.adapters.persistence.engine import (
    TransactionRunner,
    create_async_session_factory,
    create_engine,
)
from app.adapters.persistence.repositories import SqlAlchemyUnitOfWork
from app.adapters.queue import build_saq_plugin
from app.adapters.security.clock import SystemClock
from app.adapters.security.passwords import PwdlibPasswordHasher
from app.adapters.security.public_ids import SqidsPublicIdCodec
from app.adapters.web.dependencies import WebDependencies
from app.adapters.web.handlers import ROUTE_HANDLERS
from app.adapters.web.middleware import RequestEdgeMiddleware
from app.config import Settings, load_settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

PACKAGE_DIRECTORY = Path(__file__).resolve().parent
STATIC_DIRECTORY = PACKAGE_DIRECTORY / "static"
TEMPLATE_DIRECTORY = PACKAGE_DIRECTORY / "templates"


def _session_secret(secret: str) -> bytes:
    """Derive the exact AES key length required by Litestar's cookie backend."""
    return sha256(secret.encode("utf-8")).digest()


def create_app(settings: Settings | None = None) -> Litestar:
    """Compose a runnable application, allowing isolated test configuration."""
    runtime_settings = settings or load_settings()
    engine = create_engine(runtime_settings.database_url, echo=runtime_settings.debug)
    transactions = TransactionRunner(
        create_async_session_factory(engine),
        cleanup_timeout_seconds=runtime_settings.transaction_cleanup_timeout_seconds,
    )
    web_dependencies = WebDependencies(
        transactions=transactions,
        unit_of_work=SqlAlchemyUnitOfWork,
        passwords=PwdlibPasswordHasher(),
        clock=SystemClock(),
        public_ids=SqidsPublicIdCodec(
            salt=runtime_settings.sqids_salt,
            alphabet=runtime_settings.sqids_alphabet,
            min_length=runtime_settings.sqids_min_length,
        ),
        session_lifetime_seconds=runtime_settings.session_lifetime_seconds,
    )
    session_config = CookieBackendConfig(
        secret=_session_secret(runtime_settings.session_secret),
        key=runtime_settings.session_cookie_name,
        max_age=runtime_settings.session_lifetime_seconds,
        path=runtime_settings.session_cookie_path,
        secure=runtime_settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    telemetry = TelemetryRuntime(runtime_settings)
    configure_logging(
        level=runtime_settings.log_level,
        json=runtime_settings.log_format == "json",
    )
    telemetry.initialize(sqlalchemy_engine=engine)
    telemetry_plugin = telemetry.litestar_plugin()

    @asynccontextmanager
    async def database_lifespan(_: Litestar) -> AsyncGenerator[None, None]:
        try:
            async with anyio.create_task_group() as task_group:
                if telemetry.enabled:
                    task_group.start_soon(event_loop_lag_monitor, telemetry)
                try:
                    yield
                finally:
                    task_group.cancel_scope.cancel()
        finally:
            await telemetry.shutdown()
            with anyio.move_on_after(
                runtime_settings.transaction_cleanup_timeout_seconds, shield=True
            ):
                await engine.dispose()

    static_router = create_static_files_router(
        path="/static",
        directories=[STATIC_DIRECTORY],
        name="static",
        cache_control=CacheControlHeader(
            max_age=31_536_000, public=True, immutable=True
        ),
    )
    application = Litestar(
        route_handlers=[*ROUTE_HANDLERS, static_router],
        middleware=[RequestEdgeMiddleware, session_config.middleware],
        csrf_config=CSRFConfig(
            secret=runtime_settings.csrf_secret,
            cookie_name="lsstack_csrf",
            cookie_path="/",
            cookie_secure=runtime_settings.session_cookie_secure,
            cookie_httponly=False,
            cookie_samesite="lax",
        ),
        allowed_hosts=AllowedHostsConfig(
            allowed_hosts=list(runtime_settings.trusted_hosts),
            www_redirect=False,
        ),
        compression_config=CompressionConfig(backend="gzip", minimum_size=500),
        template_config=TemplateConfig(
            directory=TEMPLATE_DIRECTORY,
            engine=JinjaTemplateEngine,
        ),
        state=State(
            {
                "web_dependencies": web_dependencies,
                "engine": engine,
                "settings": runtime_settings,
                "session_config": session_config,
                "telemetry": telemetry,
            }
        ),
        plugins=[
            plugin
            for plugin in (telemetry_plugin, build_saq_plugin(runtime_settings))
            if plugin is not None
        ],
        lifespan=[database_lifespan],
        cache_control=CacheControlHeader(no_store=True),
        request_max_body_size=65_536,
        debug=runtime_settings.debug,
        openapi_config=None,
    )
    # Litestar applies its own stdlib logging configuration while constructing
    # the application. Restore the selected local renderer, then attach the
    # OTLP handler so neither is silently replaced.
    configure_logging(
        level=runtime_settings.log_level,
        json=runtime_settings.log_format == "json",
    )
    telemetry.attach_logging_handler()
    return application


app = create_app()


__all__ = ["app", "create_app"]
