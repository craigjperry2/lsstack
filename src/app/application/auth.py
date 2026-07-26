"""Registration, login, and fixed-lifetime session decisions."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.application.ports.clock import Clock
from app.application.ports.persistence import UnitOfWork
from app.application.ports.security import PasswordHasher
from app.domain.errors import (
    InvalidCredentialsError,
    InvalidSessionError,
    ValidationError,
)
from app.domain.users import User, normalize_email, validate_password


@dataclass(frozen=True, slots=True)
class AuthResult:
    user_id: int
    email_normalized: str
    session_version: int


@dataclass(frozen=True, slots=True)
class UserSession:
    user_id: int
    email_normalized: str
    session_version: int


@dataclass(frozen=True, slots=True)
class SessionClaims:
    user_id: int
    session_version: int
    issued_at: datetime
    expires_at: datetime


def register(
    uow: UnitOfWork,
    password_hasher: PasswordHasher,
    clock: Clock,
    *,
    email: str,
    password: str,
) -> AuthResult:
    """Register atomically; duplicate handling is delegated to the repository."""
    email_normalized = normalize_email(email)
    validate_password(password, email_normalized)
    now = clock.now()
    user = uow.users.add(
        User(
            id=None,
            email_normalized=email_normalized,
            password_hash=password_hasher.hash(password),
            session_version=0,
            created_at=now,
            updated_at=now,
        )
    )
    if user.id is None:
        raise RuntimeError("The user repository returned an unpersisted user.")
    return AuthResult(user.id, user.email_normalized, user.session_version)


def authenticate(
    uow: UnitOfWork,
    password_hasher: PasswordHasher,
    clock: Clock,
    *,
    email: str,
    password: str,
) -> AuthResult:
    """Authenticate without revealing whether the normalized account exists."""
    try:
        email_normalized = normalize_email(email)
    except ValidationError:
        password_hasher.verify_unknown(password)
        raise InvalidCredentialsError("Invalid email or password.") from None
    user = uow.users.get_by_email(email_normalized)
    if user is None:
        password_hasher.verify_unknown(password)
        raise InvalidCredentialsError("Invalid email or password.")
    verification = password_hasher.verify(password, user.password_hash)
    if not verification.valid:
        raise InvalidCredentialsError("Invalid email or password.")
    if verification.replacement_hash is not None:
        user = uow.users.update(
            user.with_rehashed_password(verification.replacement_hash, clock.now())
        )
    if user.id is None:
        raise RuntimeError("The user repository returned an unpersisted user.")
    return AuthResult(user.id, user.email_normalized, user.session_version)


def get_user_session(uow: UnitOfWork, *, user_id: int) -> UserSession | None:
    """Return current persisted session identity for per-request revalidation."""
    user = uow.users.get_by_id(user_id)
    if user is None or user.id is None:
        return None
    return UserSession(user.id, user.email_normalized, user.session_version)


def issue_session(
    auth: AuthResult, clock: Clock, *, lifetime: timedelta
) -> SessionClaims:
    """Create absolute, non-sliding session claims."""
    if lifetime <= timedelta(0):
        raise ValueError("Session lifetime must be positive.")
    issued_at = clock.now()
    return SessionClaims(
        user_id=auth.user_id,
        session_version=auth.session_version,
        issued_at=issued_at,
        expires_at=issued_at + lifetime,
    )


def revalidate_session(
    uow: UnitOfWork, clock: Clock, *, claims: SessionClaims
) -> UserSession:
    """Check expiry, user existence, and revocation version on every request."""
    if clock.now() >= claims.expires_at:
        raise InvalidSessionError("Session expired.")
    current = get_user_session(uow, user_id=claims.user_id)
    if current is None or current.session_version != claims.session_version:
        raise InvalidSessionError("Session is no longer valid.")
    return current
