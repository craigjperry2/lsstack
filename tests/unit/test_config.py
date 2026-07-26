from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import LOCAL_SECRET, Settings, load_settings

VALID_PRODUCTION_SETTINGS: dict[str, object] = {
    "environment": "production",
    "debug": False,
    "session_secret": "session-secret-that-is-distinct-and-long-enough-123",
    "csrf_secret": "csrf-secret-that-is-distinct-and-long-enough-456",
    "session_cookie_secure": True,
    "public_base_url": "https://example.com",
    "trusted_hosts": ("example.com",),
}


def test_local_environment_accepts_checked_in_development_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("APP_ENV", "ENVIRONMENT", "SESSION_SECRET", "CSRF_SECRET"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()

    assert settings.environment == "local"
    assert settings.session_secret == settings.csrf_secret == LOCAL_SECRET
    assert settings.session_cookie_secure is False
    assert settings.public_base_url == "http://localhost:8080"


def test_checked_in_env_example_parses_comma_separated_trusted_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)

    settings = load_settings(env_file=".env.example")

    assert settings.trusted_hosts == ("localhost", "127.0.0.1")


def test_pytest_environment_disables_telemetry_over_checked_in_example() -> None:
    settings = load_settings(env_file=".env.example")

    assert settings.telemetry_enabled is False


def test_trusted_hosts_environment_value_is_not_json_decoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTED_HOSTS", "localhost, 127.0.0.1")

    assert Settings().trusted_hosts == ("localhost", "127.0.0.1")


def test_production_rejects_checked_in_placeholder_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("SESSION_SECRET", "CSRF_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError, match="non-placeholder"):
        Settings(environment="production")


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (
            {"csrf_secret": ("session-secret-that-is-distinct-and-long-enough-123")},
            "must be independent",
        ),
        ({"session_cookie_secure": False}, "must be true"),
        ({"public_base_url": "http://example.com"}, "must use HTTPS"),
    ],
)
def test_production_rejects_unsafe_security_combinations(
    override: dict[str, object],
    message: str,
) -> None:
    values = {**VALID_PRODUCTION_SETTINGS, **override}

    with pytest.raises(ValidationError, match=message):
        Settings(**values)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("public_base_url", "localhost:8080"),
        ("proxy_base_url", "http:///missing-host"),
        ("proxy_base_url", "http://host:not-a-port"),
        ("otlp_endpoint", "grpc://collector:4317"),
        ("otlp_endpoint", "http://user:secret@collector:4317"),
        ("public_base_url", "https://example.com/?token=secret"),
    ],
)
def test_service_urls_require_safe_absolute_http_endpoints(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match=r"must|invalid"):
        Settings(**{field: value})  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", "postgresql+psycopg://db/app"),
        ("admin_database_url", "postgresql+psycopg://admin@db:not-a-port/app"),
        ("test_database_url", "postgresql://app@db/app_test"),
        ("saq_database_url", "postgresql+psycopg://saq@db/app"),
    ],
)
def test_database_urls_reject_malformed_or_wrong_driver_values(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})  # pyright: ignore[reportArgumentType]
