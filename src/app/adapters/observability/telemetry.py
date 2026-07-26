"""Configurable OTLP providers and structured event-loop lag monitoring."""

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

import anyio
from anyio import to_thread
from litestar.plugins.opentelemetry import OpenTelemetryConfig, OpenTelemetryPlugin
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.sqlalchemy import (  # pyright: ignore[reportMissingTypeStubs]
    SQLAlchemyInstrumentor,
)
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import Settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class TelemetryRuntime:
    """Owned telemetry resources; initialization is inert when disabled."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tracer_provider: TracerProvider | None = None
        self._meter_provider: MeterProvider | None = None
        self._logger_provider: LoggerProvider | None = None
        self._span_exporter: OTLPSpanExporter | None = None
        self._metric_exporter: OTLPMetricExporter | None = None
        self._log_exporter: OTLPLogExporter | None = None
        self._logging_handler: LoggingHandler | None = None
        self._sqlalchemy_instrumentor: SQLAlchemyInstrumentor | None = None
        self._last_event_loop_lag_seconds = 0.0
        self._initialized = False

    @property
    def enabled(self) -> bool:
        return self._settings.telemetry_enabled

    def _observe_lag(self, _options: CallbackOptions) -> Iterable[Observation]:
        yield Observation(self._last_event_loop_lag_seconds)

    def record_event_loop_lag(self, value: float) -> None:
        self._last_event_loop_lag_seconds = max(0.0, value)

    def initialize(self, *, sqlalchemy_engine: "AsyncEngine | None" = None) -> None:
        if self._initialized or not self.enabled:
            return
        resource = Resource.create(
            {
                "service.name": self._settings.otel_service_name,
                "deployment.environment.name": self._settings.environment,
            }
        )
        insecure = self._settings.otlp_endpoint.startswith("http://")
        span_exporter = OTLPSpanExporter(
            endpoint=self._settings.otlp_endpoint,
            insecure=insecure,
        )
        metric_exporter = OTLPMetricExporter(
            endpoint=self._settings.otlp_endpoint,
            insecure=insecure,
        )
        log_exporter = OTLPLogExporter(
            endpoint=self._settings.otlp_endpoint,
            insecure=insecure,
        )
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(metric_exporter)],
        )
        meter_provider.get_meter("app.runtime").create_observable_gauge(
            "event_loop_lag_seconds",
            callbacks=[self._observe_lag],
            unit="s",
            description="Drift beyond the event loop monitor's scheduled wake time.",
        )
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        logging_handler = LoggingHandler(
            level=logging.NOTSET, logger_provider=logger_provider
        )
        # SAQ obtains its tracer from the global provider. Litestar and our
        # explicit adapters still receive these providers directly.
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        if sqlalchemy_engine is not None:
            instrumentor = SQLAlchemyInstrumentor()
            instrumentor.instrument(
                engine=sqlalchemy_engine.sync_engine,
                tracer_provider=tracer_provider,
                enable_commenter=False,
            )
            self._sqlalchemy_instrumentor = instrumentor
        self._tracer_provider = tracer_provider
        self._meter_provider = meter_provider
        self._logger_provider = logger_provider
        self._span_exporter = span_exporter
        self._metric_exporter = metric_exporter
        self._log_exporter = log_exporter
        self._logging_handler = logging_handler
        self._initialized = True

    def attach_logging_handler(self) -> None:
        """Attach OTLP logging where framework root reconfiguration cannot remove it.

        Litestar and the worker CLI both reconfigure the root logger after the
        application is constructed.  Application loggers are all beneath the
        ``app`` namespace, whose handlers survive those root changes while
        records still propagate to the active console handler.
        """
        handler = self._logging_handler
        app_logger = logging.getLogger("app")
        if handler is not None and handler not in app_logger.handlers:
            app_logger.addHandler(handler)

    @property
    def tracer_provider(self) -> TracerProvider | None:
        return self._tracer_provider

    @property
    def meter_provider(self) -> MeterProvider | None:
        return self._meter_provider

    def litestar_plugin(self) -> OpenTelemetryPlugin | None:
        """Instrument inbound HTTP only when configured exporters are active."""
        if self._tracer_provider is None or self._meter_provider is None:
            return None
        return OpenTelemetryPlugin(
            OpenTelemetryConfig(
                tracer_provider=self._tracer_provider,
                meter_provider=self._meter_provider,
                exclude_spans=["receive", "send"],
                http_capture_headers_server_request=["x-request-id"],
                http_capture_headers_server_response=["x-request-id"],
                http_capture_headers_sanitize_fields=[
                    "authorization",
                    "cookie",
                    "set-cookie",
                    "x-csrf-token",
                ],
            )
        )

    async def shutdown(self, *, timeout_seconds: float = 5.0) -> None:
        """Bounded flush/shutdown that remains safe when LGTM is unavailable."""
        if self._logging_handler is not None:
            logging.getLogger("app").removeHandler(self._logging_handler)
            self._logging_handler.close()
            self._logging_handler = None
        if self._sqlalchemy_instrumentor is not None:
            self._sqlalchemy_instrumentor.uninstrument()
            self._sqlalchemy_instrumentor = None
        providers = tuple(
            provider
            for provider in (
                self._logger_provider,
                self._meter_provider,
                self._tracer_provider,
            )
            if provider is not None
        )
        if not providers:
            return

        def shutdown_providers() -> None:
            # Provider shutdown owns processor/reader/exporter shutdown. Calling
            # exporters first makes each provider close them a second time.
            for provider in providers:
                provider.shutdown()

        with anyio.move_on_after(timeout_seconds, shield=True):
            await to_thread.run_sync(shutdown_providers, abandon_on_cancel=True)
        self._tracer_provider = None
        self._meter_provider = None
        self._logger_provider = None
        self._span_exporter = None
        self._metric_exporter = None
        self._log_exporter = None
        self._initialized = False


async def event_loop_lag_monitor(
    telemetry: TelemetryRuntime, *, interval_seconds: float = 0.01
) -> None:
    """Measure scheduled 10 ms drift for the lifetime of an owning task group."""
    if interval_seconds <= 0:
        raise ValueError("Event-loop lag monitor interval must be positive.")
    target = anyio.current_time() + interval_seconds
    while True:
        await anyio.sleep_until(target)
        now = anyio.current_time()
        telemetry.record_event_loop_lag(now - target)
        target += interval_seconds
