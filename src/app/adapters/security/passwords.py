"""Argon2id password hashing through pwdlib."""

from pwdlib import PasswordHash

from app.application.ports.security import PasswordVerification


class PwdlibPasswordHasher:
    """Stateless facade with a precomputed hash for unknown-account verification."""

    def __init__(self, password_hash: PasswordHash | None = None) -> None:
        self._password_hash = password_hash or PasswordHash.recommended()
        self._dummy_hash = self._password_hash.hash(
            "constant-time-unknown-account-password"
        )

    def hash(self, password: str) -> str:
        return self._password_hash.hash(password)

    def verify(self, password: str, password_hash: str) -> PasswordVerification:
        valid, replacement_hash = self._password_hash.verify_and_update(
            password, password_hash
        )
        return PasswordVerification(
            valid=valid,
            replacement_hash=replacement_hash if valid else None,
        )

    def verify_unknown(self, password: str) -> None:
        self._password_hash.verify(password, self._dummy_hash)
