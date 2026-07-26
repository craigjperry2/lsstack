"""Small ASGI edge middleware for request correlation and response hardening."""

from __future__ import annotations

import re
import uuid
from time import perf_counter
from typing import TYPE_CHECKING, Final, cast

import structlog

from app.adapters.observability import bind_request_id, clear_request_id

if TYPE_CHECKING:
    from litestar.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER: Final = b"x-request-id"
_VALID_REQUEST_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LOGGER = structlog.get_logger("app.request")

SECURITY_HEADERS: Final[tuple[tuple[bytes, bytes], ...]] = (
    (
        b"content-security-policy",
        b"default-src 'self'; base-uri 'self'; form-action 'self'; "
        b"frame-ancestors 'none'; object-src 'none'; script-src 'self'; "
        b"style-src 'self'; img-src 'self'; connect-src 'self'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (
        b"permissions-policy",
        b"camera=(), display-capture=(), geolocation=(), microphone=(), "
        b"payment=(), usb=()",
    ),
)


def resolve_request_id(raw_value: str | None) -> str:
    if raw_value is not None and _VALID_REQUEST_ID.fullmatch(raw_value):
        return raw_value
    return uuid.uuid4().hex


class RequestEdgeMiddleware:
    """Validate/generate a request ID and set browser security headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_request_id = None
        request_headers = cast("list[tuple[bytes, bytes]]", scope.get("headers", []))
        for name, value in request_headers:
            if name.lower() == REQUEST_ID_HEADER:
                raw_request_id = value.decode("ascii", errors="ignore")
                break
        request_id = resolve_request_id(raw_request_id)
        token = bind_request_id(request_id)
        started_at = perf_counter()
        status_code = 500

        async def send_with_headers(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                existing_names = {name.lower() for name, _ in headers}
                if REQUEST_ID_HEADER not in existing_names:
                    headers.append((REQUEST_ID_HEADER, request_id.encode("ascii")))
                headers.extend(
                    header
                    for header in SECURITY_HEADERS
                    if header[0] not in existing_names
                )
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            try:
                LOGGER.info(
                    "request.complete",
                    request_id=request_id,
                    method=cast("str", scope.get("method", "")),
                    path=cast("str", scope.get("path", "")),
                    status=status_code,
                    duration_ms=round((perf_counter() - started_at) * 1_000, 3),
                )
            finally:
                clear_request_id(token)
