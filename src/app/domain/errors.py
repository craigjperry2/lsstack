"""Expected domain and application failures."""


class DomainError(Exception):
    """Base class for an expected outcome that an adapter may translate."""


class ValidationError(DomainError):
    """An input violates a domain invariant."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class DuplicateEmailError(DomainError):
    """The normalized login email is already registered."""


class InvalidCredentialsError(DomainError):
    """Authentication failed without disclosing which credential was wrong."""


class InvalidSessionError(DomainError):
    """A session is expired, revoked, malformed, or belongs to a missing user."""


class CurrentPasswordMismatchError(DomainError):
    """The current password supplied for a sensitive action was wrong."""


class TaskNotFoundError(DomainError):
    """A task is absent or is not owned by the current user."""
