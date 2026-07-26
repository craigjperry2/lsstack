"""Untrusted HTML-form extraction and edge validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class FormValues:
    values: dict[str, str]
    errors: tuple[str, ...] = ()


def text(form: Mapping[str, object], key: str) -> str:
    value = form.get(key, "")
    return value if isinstance(value, str) else ""


def task_form(form: Mapping[str, object]) -> FormValues:
    title = text(form, "title").strip()
    description = text(form, "description").strip()
    errors: list[str] = []
    if not title:
        errors.append("Title is required.")
    elif len(title) > 200:
        errors.append("Title must be 200 characters or fewer.")
    if len(description) > 5_000:
        errors.append("Description must be 5,000 characters or fewer.")
    return FormValues(
        values={"title": title, "description": description},
        errors=tuple(errors),
    )


def registration_form(form: Mapping[str, object]) -> FormValues:
    email = text(form, "email").strip()
    password = text(form, "password")
    confirmation = text(form, "password_confirmation")
    errors = () if password == confirmation else ("The passwords do not match.",)
    return FormValues(
        values={
            "email": email,
            "password": password,
            "password_confirmation": confirmation,
        },
        errors=errors,
    )


def password_change_form(form: Mapping[str, object]) -> FormValues:
    current = text(form, "current_password")
    new = text(form, "new_password")
    confirmation = text(form, "new_password_confirmation")
    errors = () if new == confirmation else ("The new passwords do not match.",)
    return FormValues(
        values={
            "current_password": current,
            "new_password": new,
            "new_password_confirmation": confirmation,
        },
        errors=errors,
    )
