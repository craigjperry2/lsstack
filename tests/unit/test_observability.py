from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from app.adapters.observability.telemetry import TelemetryRuntime
from app.config import Settings

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LoggingHandler


def test_otel_handler_survives_root_reconfiguration_for_web_and_worker() -> None:
    records: list[logging.LogRecord] = []

    class RecordingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    runtime = TelemetryRuntime(Settings())
    handler = cast("LoggingHandler", RecordingHandler())
    runtime._logging_handler = handler  # pyright: ignore[reportPrivateUsage]
    app_logger = logging.getLogger("app")
    root_logger = logging.getLogger()
    replacement = logging.NullHandler()
    previous_root_handlers = root_logger.handlers[:]
    try:
        runtime.attach_logging_handler()
        assert handler in app_logger.handlers

        # Litestar does this while constructing the app and the worker CLI does
        # it again before running SAQ. A root-attached OTLP handler was lost.
        root_logger.handlers = [replacement]
        assert handler in app_logger.handlers
        logging.getLogger("app.outbox").warning("worker probe")
        assert [record.name for record in records] == ["app.outbox"]
    finally:
        app_logger.removeHandler(handler)
        root_logger.handlers = previous_root_handlers
