"""Small serialization helpers for Litestar's encrypted session mapping."""

from datetime import UTC, datetime
from typing import cast

from app.application.auth import SessionClaims

type SessionScalar = str | int | float | bool | None


def claims_to_session(claims: SessionClaims) -> dict[str, SessionScalar]:
    return {
        "user_id": claims.user_id,
        "session_version": claims.session_version,
        "issued_at": claims.issued_at.timestamp(),
        "expires_at": claims.expires_at.timestamp(),
    }


def claims_from_session(value: object) -> SessionClaims | None:
    if not isinstance(value, dict):
        return None
    mapping = cast("dict[str, object]", value)
    user_id = mapping.get("user_id")
    version = mapping.get("session_version")
    issued = mapping.get("issued_at")
    expires = mapping.get("expires_at")
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id < 1
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 0
        or not isinstance(issued, int | float)
        or isinstance(issued, bool)
        or not isinstance(expires, int | float)
        or isinstance(expires, bool)
        or expires <= issued
    ):
        return None
    try:
        issued_at = datetime.fromtimestamp(issued, UTC)
        expires_at = datetime.fromtimestamp(expires, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return SessionClaims(user_id, version, issued_at, expires_at)
