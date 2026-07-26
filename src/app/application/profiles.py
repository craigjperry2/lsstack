"""Profile use cases."""

from app.application.ports.clock import Clock
from app.application.ports.persistence import UnitOfWork
from app.application.ports.security import PasswordHasher
from app.domain.errors import (
    CurrentPasswordMismatchError,
    InvalidSessionError,
    ValidationError,
)
from app.domain.users import validate_password


def change_password(
    uow: UnitOfWork,
    password_hasher: PasswordHasher,
    clock: Clock,
    *,
    user_id: int,
    current_password: str,
    new_password: str,
    new_password_confirmation: str,
) -> int:
    """Replace a password and revoke all previous session versions."""
    user = uow.users.get_by_id(user_id)
    if user is None:
        raise InvalidSessionError("The authenticated user no longer exists.")
    verification = password_hasher.verify(current_password, user.password_hash)
    if not verification.valid:
        raise CurrentPasswordMismatchError("Current password is incorrect.")
    if new_password != new_password_confirmation:
        raise ValidationError("new_password_confirmation", "Passwords do not match.")
    validate_password(new_password, user.email_normalized)
    updated = uow.users.update(
        user.with_password(password_hasher.hash(new_password), clock.now())
    )
    return updated.session_version
