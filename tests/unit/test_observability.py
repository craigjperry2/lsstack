from __future__ import annotations

import atexit
import importlib
import logging
import sys
import threading
from logging.handlers import QueueListener
from typing import TYPE_CHECKING, cast

import pytest

from app.adapters.observability import telemetry as telemetry_module
from app.adapters.observability.telemetry import TelemetryRuntime
from app.config import Settings

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LoggingHandler


def test_explicitly_disabled_telemetry_is_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_exporter_construction(*args: object, **kwargs: object) -> None:
        pytest.fail(
            f"disabled telemetry constructed an OTLP exporter: {args!r} {kwargs!r}"
        )

    monkeypatch.setattr(
        telemetry_module, "OTLPSpanExporter", fail_exporter_construction
    )
    monkeypatch.setattr(
        telemetry_module, "OTLPMetricExporter", fail_exporter_construction
    )
    monkeypatch.setattr(telemetry_module, "OTLPLogExporter", fail_exporter_construction)
    threads_before = set(threading.enumerate())
    runtime = TelemetryRuntime(Settings(telemetry_enabled=False))

    runtime.initialize()

    assert runtime.enabled is False
    assert runtime.tracer_provider is None
    assert runtime.meter_provider is None
    assert runtime.litestar_plugin() is None
    assert set(threading.enumerate()) == threads_before


def test_importing_module_app_does_not_construct_exporters_or_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_exporter_construction(*args: object, **kwargs: object) -> None:
        pytest.fail(f"app import constructed an OTLP exporter: {args!r} {kwargs!r}")

    monkeypatch.setattr(
        telemetry_module, "OTLPSpanExporter", fail_exporter_construction
    )
    monkeypatch.setattr(
        telemetry_module, "OTLPMetricExporter", fail_exporter_construction
    )
    monkeypatch.setattr(telemetry_module, "OTLPLogExporter", fail_exporter_construction)
    root_logger = logging.getLogger()
    app_logger = logging.getLogger("app")
    app_package = sys.modules["app"]
    missing_package_attribute = object()
    previous_package_main = app_package.__dict__.get("main", missing_package_attribute)
    previous_root_handlers = root_logger.handlers[:]
    previous_app_handlers = app_logger.handlers[:]
    previous_root_level = root_logger.level
    previous_app_level = app_logger.level
    previous_app_propagate = app_logger.propagate
    previous_module = sys.modules.pop("app.main", None)
    threads_before = set(threading.enumerate())
    new_threads: set[threading.Thread] = set()
    new_listeners: set[QueueListener] = set()
    try:
        main_module = importlib.import_module("app.main")
        runtime = main_module.app.state.telemetry
        new_threads = set(threading.enumerate()) - threads_before
        for thread in new_threads:
            thread_target = getattr(thread, "_target", None)
            listener = getattr(thread_target, "__self__", None)
            if isinstance(listener, QueueListener):
                new_listeners.add(listener)
        otel_threads = [
            thread for thread in new_threads if thread.name.startswith("Otel")
        ]

        assert runtime.enabled is False
        assert runtime.tracer_provider is None
        assert runtime.meter_provider is None
        assert otel_threads == []
    finally:
        sys.modules.pop("app.main", None)
        if previous_module is not None:
            sys.modules["app.main"] = previous_module
        if previous_package_main is missing_package_attribute:
            app_package.__dict__.pop("main", None)
        else:
            app_package.__dict__["main"] = previous_package_main
        for listener in new_listeners:
            atexit.unregister(listener.stop)
            listener.stop()
        root_logger.handlers = previous_root_handlers
        root_logger.setLevel(previous_root_level)
        app_logger.handlers = previous_app_handlers
        app_logger.setLevel(previous_app_level)
        app_logger.propagate = previous_app_propagate


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
