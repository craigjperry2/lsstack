"""Detached values used by the HTTP adapter and its templates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: int
    email: str
    session_version: int


@dataclass(frozen=True, slots=True)
class TaskView:
    public_id: str
    title: str
    description: str | None
    is_completed: bool
    background_processed_at: datetime | None
    created_at: datetime
