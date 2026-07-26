"""Structured logging with request and trace correlation."""

import contextvars
import logging
import sys

import structlog
from opentelemetry import trace

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


def bind_request_id(request_id: str) -> contextvars.Token[str | None]:
    return _request_id.set(request_id)


def clear_request_id(token: contextvars.Token[str | None]) -> None:
    _request_id.reset(token)


def add_correlation(
    _logger: object,
    _method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    request_id = _request_id.get()
    if request_id is not None:
        event_dict["request_id"] = request_id
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = f"{span_context.trace_id:032x}"
        event_dict["span_id"] = f"{span_context.span_id:016x}"
    return event_dict


def configure_logging(*, level: str, json: bool) -> None:
    """Configure once at startup; callers control JSON versus local console output."""
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        add_correlation,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        renderer,
    ]
    numeric_level = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }.get(level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=numeric_level,
        force=True,
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
