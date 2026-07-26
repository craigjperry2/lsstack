"""Password and public identifier ports."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PasswordVerification:
    valid: bool
    replacement_hash: str | None = None


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str:
        """Hash a validated password."""
        ...

    def verify(self, password: str, password_hash: str) -> PasswordVerification:
        """Verify a password and optionally return an upgraded hash."""
        ...

    def verify_unknown(self, password: str) -> None:
        """Perform equivalent work when no account exists."""
        ...


class PublicIdCodec(Protocol):
    def encode(self, internal_id: int) -> str:
        """Encode a positive internal identifier."""
        ...

    def decode(self, public_id: str) -> int | None:
        """Decode one identifier, returning None for malformed input."""
        ...
