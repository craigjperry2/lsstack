"""User primitives and authentication validation."""

import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime

from app.domain.errors import ValidationError

MAX_EMAIL_LENGTH = 320
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128


def normalize_email(value: str) -> str:
    """Return the single canonical login representation for an email."""
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if (
        not normalized
        or len(normalized) > MAX_EMAIL_LENGTH
        or normalized.count("@") != 1
        or any(character.isspace() for character in normalized)
    ):
        raise ValidationError("email", "Enter a valid email address.")
    local, domain = normalized.split("@", maxsplit=1)
    if not local or not domain or "." not in domain:
        raise ValidationError("email", "Enter a valid email address.")
    return normalized


def validate_password(password: str, email_normalized: str) -> None:
    """Apply intentionally small, Unicode-aware password rules."""
    if len(password) < MIN_PASSWORD_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        raise ValidationError(
            "password",
            f"Password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters.",
        )
    if password != password.strip():
        raise ValidationError(
            "password", "Password must not start or end with whitespace."
        )
    comparable = unicodedata.normalize("NFKC", password).casefold()
    if email_normalized in comparable:
        raise ValidationError(
            "password", "Password must not contain your email address."
        )


@dataclass(frozen=True, slots=True)
class User:
    """Detached immutable user state."""

    id: int | None
    email_normalized: str
    password_hash: str
    session_version: int
    created_at: datetime
    updated_at: datetime

    def with_password(self, password_hash: str, changed_at: datetime) -> "User":
        return replace(
            self,
            password_hash=password_hash,
            session_version=self.session_version + 1,
            updated_at=changed_at,
        )

    def with_rehashed_password(
        self, password_hash: str, changed_at: datetime
    ) -> "User":
        return replace(self, password_hash=password_hash, updated_at=changed_at)
