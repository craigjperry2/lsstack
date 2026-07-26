# lsstack

`lsstack` is a concrete Python 3.12 starter for server-rendered Litestar
applications. It includes registration and encrypted-cookie login, password
changes, user-owned task CRUD, HTMX fragments, a transactional outbox with a
PostgreSQL-backed SAQ worker, and local OpenTelemetry observability.

This repository is intentionally local-first. Python and the SAQ worker run on
the host. Docker Compose supplies PostgreSQL 17, Nginx, Grafana's all-in-one
LGTM development stack, and an OpenTelemetry Collector that tails Nginx logs.
There is no application image or production deployment configuration.

## Prerequisites

- Nix with flakes enabled on NixOS, nix-darwin, or another supported Linux or
  macOS installation
- Docker Engine with the Compose v2 plugin, or Docker Desktop

The flake supports `x86_64-linux`, `aarch64-linux`, `x86_64-darwin`, and
`aarch64-darwin`. It exposes only Python 3.12 and `uv`; `uv` owns `.venv` and
all Python developer tools.

## First run

Create a local environment file, enter the development shell, and install the
exact locked dependency set:

```console
cp .env.example .env
nix develop
uv sync --all-groups --frozen
```

The checked-in secrets and passwords are deliberately local-only. Session
cookies use `Secure=false` because local Nginx serves plain HTTP. Do not reuse
these settings outside an isolated development machine.

Start the dependency stack and migrate the application schema:

```console
docker compose up -d --wait
uv run alembic upgrade head
```

Run Litestar/Uvicorn on the host:

```console
uv run litestar run --reload --host 0.0.0.0 --port 8000
```

In a second `nix develop` shell, start the configured SAQ worker:

```console
uv run litestar workers run
```

Open the application through Nginx at
[http://localhost:8080](http://localhost:8080). Do not browse directly to port
8000: going through Nginx exercises request-ID propagation, security headers,
static caching, and access-log collection.

Grafana is available at [http://localhost:3000](http://localhost:3000). The
LGTM image is a development observability backend, not a production monitoring
installation.

## What runs where

```text
browser -> Nginx :8080 -> Litestar on host :8000 -> PostgreSQL :5432
                               |                    |- app schema
                               |                    `- saq schema
                               |
                               `-> OTLP :4317/:4318 -> LGTM -> Grafana :3000

Nginx JSON log volume -> collector-contrib -> LGTM
SAQ worker on host -> outbox relay and jobs -> PostgreSQL + OTLP
```

All published container ports bind to `127.0.0.1`. Compose maps
`host.docker.internal` to Docker's host gateway on Linux while preserving
Docker Desktop's native macOS behavior.

PostgreSQL uses one local database with two isolated login roles:

- `app_user` owns and can use only the `app` schema.
- `saq_user` owns and can use only the `saq` schema.

Alembic owns only the `app` schema. SAQ initializes and owns its private tables
in `saq`; do not add SAQ's internal tables to Alembic migrations.

## Everyday commands

Run the fast database-free suite:

```console
uv run pytest tests/architecture tests/unit
```

Running `uv run pytest` also exercises the in-process web and performance
checks, but deliberately skips destructive database tests unless they are
explicitly enabled.

For the database-backed suite, start Compose and export the admin, application,
SAQ, and `_test` database URLs from the local environment file before opting
in:

```console
docker compose up -d --wait
set -a
. ./.env
set +a
RUN_DATABASE_TESTS=1 uv run pytest
```

The fixture refuses destructive setup unless `TEST_DATABASE_URL` names a
disposable database ending in `_test`. It uses `ADMIN_DATABASE_URL` only to
create and drop that database, then connects through the isolated application
and SAQ roles. Never point these variables at shared data.

To include the real Nginx-to-web-to-outbox-to-worker checks, also start the host
application and worker as described above, then run:

```console
RUN_DATABASE_TESTS=1 RUN_SERVICE_TESTS=1 uv run pytest
```

The service smoke test creates a uniquely named local user and task in the
normal `lsstack` development database and waits with a bounded poll for the
worker's persisted result.

Run the database-free checks:

```console
uv run pytest tests/architecture tests/unit
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
uv run lint-imports
```

Format and apply safe lint fixes:

```console
uv run ruff format .
uv run ruff check --fix .
```

Install and exercise the repository hooks:

```console
uv run prek install --hook-type pre-commit --hook-type pre-push
uv run prek run --all-files
uv run prek run --all-files --hook-stage pre-push
```

Inspect service state and logs:

```console
docker compose ps
docker compose logs -f postgres nginx nginx-log-collector lgtm
```

Stop the local services without deleting their data:

```console
docker compose down --remove-orphans
```

To reset all local PostgreSQL and LGTM state, stop the host application and
worker first, then remove the named volumes. This permanently deletes the
local development database:

```console
docker compose down --volumes --remove-orphans
docker compose up -d --wait
uv run alembic upgrade head
```

## Database changes

After changing a SQLAlchemy model, generate a candidate migration:

```console
uv run alembic revision --autogenerate -m "describe the change"
```

Review and edit the generated migration before running it. In particular,
check schema names, deletion policies, constraints, indexes, data movement,
and whether an apparent drop/add should instead be a rename. Then verify both
directions against a disposable local database:

```console
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Commit the reviewed migration with its model change.

## Reference behavior

Registration normalizes the email address, creates the user atomically, issues
a fixed 12-hour encrypted session, and redirects to the task list. Password
changes require the current password, increment the stored session version,
revoke old cookies, and issue a replacement to the current browser.

Task URLs contain salted Sqids, never database primary keys. Every task query
is scoped to the authenticated owner. Task creation writes a versioned
`task.created.v1` outbox message in the same transaction and returns without
waiting for background work. The worker periodically relays committed messages
to SAQ with the outbox UUID as its stable job key. The example job waits
asynchronously for 200 ms and idempotently marks the task processed; only
pending rows poll their status.

Every unsafe form is protected by Litestar's signed double-submit CSRF
middleware. Nginx validates or generates request IDs, disables dynamic response
caching, and gives only versioned local static assets immutable cache headers.
Pico CSS 2.1.1 and HTMX 2.0.10 are vendored under `src/app/static/vendor`; page
rendering makes no CDN requests. Their upstream sources, checksums, and licenses
are recorded in that directory.

## Observability

When `TELEMETRY_ENABLED=true`, the application and worker export OTLP to the
local LGTM receiver. Startup remains usable if LGTM is unavailable. The
provisioned `lsstack local overview` dashboard includes request rate, HTTP
errors, duration, event-loop lag, queue activity, application/Nginx logs, and
recent traces. A provisioned alert waits 30 seconds before firing when measured
event-loop lag remains above 20 ms.

The Nginx access log contains the request ID, method, path without its query
string, response status and size, timings, user agent, and safe forwarding
information. It never records cookies, authorization, form bodies, passwords,
CSRF values, or full query strings. Only this structured JSON access log is
shipped to LGTM. Native runtime errors are written to container stderr at
`warn` severity and are available with `docker compose logs nginx`; they are
not added to the filelog collector. Stock Nginx error lines are not JSON and
can contain the full request target. This is a local diagnostic stream, so do
not put secrets in query strings. See the
[initial bootstrap deviations](docs/plans/initial-bootstrap-deviations.md) for
the conservative security decision.

## Troubleshooting

If `docker compose up --wait` fails, inspect `docker compose ps` and the
unhealthy service's logs. PostgreSQL initialization scripts run only when its
data volume is empty; after changing local roles or the init script, reset the
volumes as described above.

If Nginx returns `502 Bad Gateway`, confirm that Litestar is listening on
`0.0.0.0:8000` and that Docker supports the configured host-gateway mapping.
Inspect `docker compose logs nginx` for the native upstream connection error.
Only the structured access log is shipped to LGTM; native error lines remain
on container stderr, are not JSON, and can contain the full request target, so
do not put secrets in query strings.
The `/nginx-health` endpoint proves only that Nginx itself is running; `/livez`
and `/readyz` are application liveness and dependency-readiness checks.

If login works with an HTTP client but not a browser, confirm `.env` still has
`SESSION_COOKIE_SECURE=false` for local HTTP and that you are browsing
`http://localhost:8080`. Clear old `localhost` cookies after changing session
or CSRF secrets.

If telemetry is absent, confirm `TELEMETRY_ENABLED=true`, check the application,
worker, collector, and LGTM logs, then generate traffic through Nginx. Exporters
retry with bounded queues, so LGTM can start after the host processes.

## Manual browser smoke check

Before handing off a change:

1. Register a mixed-case email and confirm the new session opens the task list.
2. Create, edit, complete, and delete tasks; check useful validation and empty
   states.
3. Create a task and watch its pending row become processed without a full-page
   reload.
4. Open a second authenticated browser session, change the password in the
   first, and confirm the old session is rejected while the initiating session
   remains signed in.
5. Check keyboard focus, form labels, narrow-screen layout, Pico styling, and
   that the browser loads no CDN assets.
6. Follow one Nginx request ID through Grafana logs and its associated
   application trace and metrics.
