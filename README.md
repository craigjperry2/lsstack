# lsstack

An opinionated Python starter architecture for server-rendered web applications.

## Architecture

- **Python:** Python 3.12; `uv` and `pyproject.toml` for dependencies; a
  `src/<package_name>/__init__.py` package layout; basedpyright for type
  checking; pytest for tests; and prek for pre-commit hooks.
- **Boundaries:** Strongly typed domain interfaces and ports-and-adapters
  separation. Async Python is confined to infrastructure edges; the domain and
  application core remain synchronous, enforced by AST-based tests.
- **Web edge:** Litestar ASGI behind an Nginx caching reverse proxy, with
  SQLAlchemy integration and the cryptography extra for secure cookies.
  Pydantic provides type-safe model validation; Jinja2 renders pages and HTMX
  partials.
- **Security:** HTTPS-only encrypted cookie sessions, Argon2 password hashing,
  and CSRF protection.
- **Persistence:** PostgreSQL 17, psycopg 3 in async mode, SQLAlchemy 2, and
  Alembic. Migrations run before the web process in local development and in an
  init container in staging and production. Sqids are used at the public-ID
  boundary.
- **Deployment:** Docker Compose for local development; Kubernetes clusters and
  pods for staging and production.
- **Observability:** OpenTelemetry Collector, Tempo, Prometheus, Loki, and
  Grafana. Structured JSON application logs and Nginx logs are shipped to Loki;
  HTTP traces and metrics are exported over OTLP and correlated in Grafana.
- **CI:** GitHub Actions builds the project and publishes containers to the
  GitHub Container Registry.

## Production configuration

For a real deployment, set `APP_ENV=production`, terminate TLS, leave
`SESSION_COOKIE_SECURE=true`, and replace `SESSION_SECRET`, `CSRF_SECRET`, and
`SQIDS_SALT`. Production startup rejects the committed development secrets.

If overriding `POSTGRES_USER`, `POSTGRES_PASSWORD`, or `POSTGRES_DB`, also
provide a matching `DATABASE_URL`. URL-delimiter characters in credentials must
be percent-encoded in that URL.

## Local development

Install dependencies:

```console
uv sync --all-groups
```

Start PostgreSQL 17 (it is bound only to localhost), then run migrations and
the development server:

```console
docker compose up -d --wait db
DATABASE_URL=postgresql+psycopg://lsstack:lsstack@localhost:5432/lsstack \
  uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://lsstack:lsstack@localhost:5432/lsstack \
SESSION_COOKIE_SECURE=false \
  uv run granian --interface asgi --host 127.0.0.1 --port 8000 app.main:app
```

## Verification

The test fixture creates a fresh database whose name must end in `_test`,
applies every Alembic migration, truncates data between tests, and drops the
database afterward:

```console
docker compose up -d --wait db
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
docker compose config --quiet
```

Override `TEST_DATABASE_URL` when PostgreSQL is on another host or port. The
database role must be allowed to create and drop the disposable test database.

## Architecture decisions

### Sqids salt

Sqids uses a custom alphabet and salt. The Python API accepts an alphabet and
minimum length, but not a salt, so derive a deterministic, salt-specific
permutation of the configured alphabet before constructing the encoder. IDs
remain stable for a given configuration, while changing the salt changes the
public-ID mapping without adding a second encryption layer.

### CSRF protection

Encrypted, HTTP-only, SameSite cookies protect authentication, but cookie
attributes alone do not validate state-changing form or HTMX requests. Enable
Litestar's signed double-submit CSRF middleware for every unsafe method and
include its `_csrf_token` in every HTML form. Reject missing or mismatched
tokens before they reach a route handler.

### Secure cookies in local Compose

Session cookies default to `secure=True`, but local Compose uses plain HTTP on
port 8080, over which browsers do not return Secure cookies. Set
`SESSION_COOKIE_SECURE=false` only in local Compose. Production must terminate
TLS and must not inherit this override.

### Database change workflow

After changing a Python model:

```text
alembic revision --autogenerate → review SQL → edit → run locally → commit
```

Reviewing the generated SQL catches unsafe translations such as dropping and
adding a column when the intended migration is a column rename.

## Code quality and correctness

Async code at the infrastructure edges is governed by these controls:

- **Linting:** Ruff's `ASYNC` rules run in CI and prek hooks.
- **Event-loop health:** A lightweight background task measures the difference
  between a scheduled 10 ms sleep and its actual wake time. Prometheus exposes
  this as `event_loop_lag_seconds`; Grafana alerts above 20 ms so blocking code
  is detected before pods fail readiness probes.
- **Structured concurrency:** An AST test bans `asyncio.create_task`. All
  in-process concurrent tasks must use Python 3.11+ `asyncio.TaskGroup` or
  `anyio.create_task_group()`.
- **Queue boundary:** Dedicated pytest performance tests enforce a 100 ms
  threshold. Longer work must run in dedicated worker deployments through SAQ
  (`litestar-saq`) with a PostgreSQL backend, not as an in-process background
  task.

### Cancellation safety

A client disconnect or Nginx timeout propagates `asyncio.CancelledError` through
the ASGI server. To prevent interrupted cleanup, poisoned connection pools, or
corrupt state:

- Acquire and release database sessions, HTTP client sessions, Redis locks, and
  similar resources with async context managers.
- Protect non-cancellable work—such as audit logging, transaction commits, and
  distributed-lock release—with `anyio.CancelScope(shield=True)`.
- Because `CancelledError` inherits from `BaseException`, ordinary `Exception`
  handlers do not intercept it. An AST test rejects `BaseException` handlers
  unless they explicitly re-raise `CancelledError`.

### Shared-state safety

Concurrent requests must not mutate shared process state:

- Use Litestar dependency injection for request-scoped services. Services must
  be short-lived or stateless singletons.
- Make domain primitives immutable with Pydantic `frozen=True` models or
  `@dataclass(frozen=True)`, checked by basedpyright in strict mode.
- Use `contextvars.ContextVar` for implicit call-stack state such as trace or
  tenant IDs. Do not use process globals or thread-local storage.

### Import boundaries

Import Linter prevents the domain from depending on adapters or async
frameworks:

```ini
# .importlinter
[importlinter]
root_package = app

[importlinter:contract:1]
name = Domain layer must not depend on adapters or async frameworks
type = forbidden
forbidden_modules =
    sqlalchemy
    asyncio
    litestar
    httpx
layers =
    app.domain
```
