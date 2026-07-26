"""Typed runtime configuration loaded explicitly by application startup."""

from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import (
    AliasChoices,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_SQIDS_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)
LOCAL_SECRET = "local-development-only-secret-change-me"  # noqa: S105


class Settings(BaseSettings):
    """Runtime configuration loaded only at the composition-root boundary."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    environment: Literal["local", "test", "development", "production"] = Field(
        default="local",
        validation_alias=AliasChoices("environment", "APP_ENV", "ENVIRONMENT"),
    )
    debug: bool = True
    database_url: str = "postgresql+psycopg://app_user:app_password@localhost:5432/app"
    admin_database_url: str = (
        "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
    )
    test_database_url: str = (
        "postgresql+psycopg://app_user:app_password@localhost:5432/app_test"
    )
    saq_database_url: str = "postgresql://saq_user:saq_password@localhost:5432/app"
    session_secret: str = LOCAL_SECRET
    csrf_secret: str = LOCAL_SECRET
    session_cookie_name: str = "lsstack_session"
    session_cookie_secure: bool = False
    session_cookie_path: str = "/"
    session_lifetime_seconds: int = Field(
        default=43_200,
        ge=1,
        le=43_200,
        validation_alias=AliasChoices(
            "session_lifetime_seconds",
            "SESSION_COOKIE_LIFETIME_SECONDS",
            "SESSION_LIFETIME_SECONDS",
        ),
    )
    sqids_salt: str = "local-lsstack-sqids-salt"
    sqids_alphabet: str = DEFAULT_SQIDS_ALPHABET
    sqids_min_length: int = Field(default=8, ge=1, le=64)
    public_base_url: str = "http://localhost:8080"
    proxy_base_url: str = "http://host.docker.internal:8000"
    trusted_hosts: Annotated[tuple[str, ...], NoDecode] = (
        "localhost",
        "127.0.0.1",
    )
    telemetry_enabled: bool = False
    otlp_endpoint: str = "http://127.0.0.1:4317"
    otel_service_name: str = "lsstack"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "console"
    transaction_cleanup_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    outbox_lease_seconds: int = Field(default=30, ge=1, le=3_600)
    outbox_batch_size: int = Field(default=25, ge=1, le=1_000)

    @field_validator(
        "database_url",
        "admin_database_url",
        "test_database_url",
        "saq_database_url",
    )
    @classmethod
    def validate_postgres_url(cls, value: str, info: ValidationInfo) -> str:
        field_name = info.field_name or "database_url"
        try:
            parsed = urlparse(value)
            port = parsed.port
        except ValueError as error:
            raise ValueError(
                f"{field_name.upper()} contains an invalid host or port."
            ) from error
        if field_name == "saq_database_url":
            if parsed.scheme != "postgresql":
                raise ValueError(
                    "SAQ_DATABASE_URL must start with postgresql://; "
                    "+psycopg is an SQLAlchemy-only driver suffix."
                )
        elif parsed.scheme != "postgresql+psycopg":
            raise ValueError(
                f"{field_name.upper()} must start with postgresql+psycopg://."
            )
        if (
            not parsed.hostname
            or not parsed.username
            or not parsed.path.strip("/")
            or parsed.fragment
            or (port is not None and port < 1)
        ):
            raise ValueError(
                "Database URLs require a user, host, valid port, and database name "
                "and must not contain a fragment."
            )
        return value

    @field_validator("sqids_alphabet")
    @classmethod
    def validate_sqids_alphabet(cls, value: str) -> str:
        if len(value) < 3 or len(set(value)) != len(value):
            raise ValueError("SQIDS_ALPHABET requires at least 3 unique characters.")
        return value

    @field_validator("sqids_salt")
    @classmethod
    def validate_sqids_salt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("SQIDS_SALT must not be empty.")
        return value

    @field_validator("public_base_url", "proxy_base_url", "otlp_endpoint")
    @classmethod
    def validate_service_url(cls, value: str, info: ValidationInfo) -> str:
        field_name = (info.field_name or "service_url").upper()
        try:
            parsed = urlparse(value)
            port = parsed.port
        except ValueError as error:
            raise ValueError(
                f"{field_name} contains an invalid host or port."
            ) from error
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(
                f"{field_name} must be an absolute HTTP(S) URL with a host."
            )
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (port is not None and port < 1)
        ):
            raise ValueError(
                f"{field_name} must use a valid port and must not contain "
                "credentials, a query, or a fragment."
            )
        return value

    @field_validator("trusted_hosts", mode="before")
    @classmethod
    def parse_trusted_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @model_validator(mode="after")
    def validate_security_configuration(self) -> "Settings":
        local = self.environment in {"local", "test", "development"}
        placeholder_markers = ("change-me", "changeme", "placeholder")
        if not local:
            for field_name, secret in (
                ("SESSION_SECRET", self.session_secret),
                ("CSRF_SECRET", self.csrf_secret),
            ):
                lowered = secret.casefold()
                if (
                    len(secret) < 32
                    or lowered.startswith("local-")
                    or any(marker in lowered for marker in placeholder_markers)
                ):
                    raise ValueError(
                        f"{field_name} must be a non-placeholder value of at least "
                        "32 characters outside local environments."
                    )
            if self.session_secret == self.csrf_secret:
                raise ValueError(
                    "SESSION_SECRET and CSRF_SECRET must be independent values "
                    "outside local environments."
                )
            if not self.session_cookie_secure:
                raise ValueError(
                    "SESSION_COOKIE_SECURE must be true outside local environments."
                )
            if urlparse(self.public_base_url).scheme != "https":
                raise ValueError(
                    "PUBLIC_BASE_URL must use HTTPS outside local environments."
                )
        if not self.trusted_hosts:
            raise ValueError("TRUSTED_HOSTS must contain at least one host.")
        return self


def load_settings(*, env_file: str | None = ".env") -> Settings:
    """Read settings once at the explicit composition-root boundary."""
    return Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue]
