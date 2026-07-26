from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from app.adapters.web.forms import (
    password_change_form,
    registration_form,
    task_form,
)
from app.adapters.web.middleware import resolve_request_id


def test_registration_confirmation_is_checked_at_edge() -> None:
    result = registration_form(
        {
            "email": " Person@Example.com ",
            "password": "valid-password",
            "password_confirmation": "different-password",
        }
    )
    assert result.values["email"] == "Person@Example.com"
    assert result.errors == ("The passwords do not match.",)


def test_task_form_trims_and_enforces_lengths() -> None:
    assert task_form({"title": "  Ship it ", "description": " notes "}).values == {
        "title": "Ship it",
        "description": "notes",
    }
    assert task_form({"title": " "}).errors == ("Title is required.",)
    assert task_form({"title": "x" * 201}).errors
    assert task_form({"title": "ok", "description": "x" * 5_001}).errors


def test_password_change_confirmation_is_checked_at_edge() -> None:
    result = password_change_form(
        {
            "current_password": "old-password",
            "new_password": "new-password",
            "new_password_confirmation": "not-the-same",
        }
    )
    assert result.errors == ("The new passwords do not match.",)


def test_request_id_accepts_only_a_narrow_safe_alphabet() -> None:
    assert resolve_request_id("trace_123.example-safe") == "trace_123.example-safe"
    for invalid in ("", "has spaces", "../path", "line\nbreak", "x" * 129):
        generated = resolve_request_id(invalid)
        assert generated != invalid
        assert len(generated) == 32


def test_vendored_asset_versions_and_checksums_are_exact() -> None:
    vendor = Path(__file__).parents[2] / "src/app/static/vendor"
    expected = {
        "pico-2.1.1.min.css": (
            "fbc9a63fc9fc9f72d12fd7fc9806e11fa9f77ae4f9cad146b27003a1119ba3db"
        ),
        "htmx-2.0.10.min.js": (
            "71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de"
        ),
    }
    for filename, digest in expected.items():
        assert sha256((vendor / filename).read_bytes()).hexdigest() == digest
    metadata = (vendor / "README.md").read_text()
    assert "Pico" not in metadata or "2.1.1" in metadata
    assert "2.0.10" in metadata
    assert (vendor / "PICO-LICENSE.md").is_file()
    assert (vendor / "HTMX-LICENSE").is_file()
