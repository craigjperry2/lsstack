"""User-owned task primitives."""

from dataclasses import dataclass, replace
from datetime import datetime

from app.domain.errors import ValidationError

MAX_TASK_TITLE_LENGTH = 200
MAX_TASK_DESCRIPTION_LENGTH = 5_000


def normalize_title(value: str) -> str:
    title = value.strip()
    if not title:
        raise ValidationError("title", "A task title is required.")
    if len(title) > MAX_TASK_TITLE_LENGTH:
        raise ValidationError(
            "title", f"Task titles may not exceed {MAX_TASK_TITLE_LENGTH} characters."
        )
    return title


def normalize_description(value: str | None) -> str | None:
    if value is None:
        return None
    description = value.strip()
    if len(description) > MAX_TASK_DESCRIPTION_LENGTH:
        raise ValidationError(
            "description",
            "Task descriptions may not exceed "
            f"{MAX_TASK_DESCRIPTION_LENGTH} characters.",
        )
    return description or None


@dataclass(frozen=True, slots=True)
class Task:
    """Detached immutable task state."""

    id: int | None
    user_id: int
    title: str
    description: str | None
    is_completed: bool
    background_processed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def edited(
        self, title: str, description: str | None, changed_at: datetime
    ) -> "Task":
        return replace(
            self,
            title=normalize_title(title),
            description=normalize_description(description),
            updated_at=changed_at,
        )

    def with_completion(self, *, completed: bool, changed_at: datetime) -> "Task":
        return replace(self, is_completed=completed, updated_at=changed_at)

    def with_background_processed(self, processed_at: datetime) -> "Task":
        if self.background_processed_at is not None:
            return self
        return replace(
            self,
            background_processed_at=processed_at,
            updated_at=processed_at,
        )
