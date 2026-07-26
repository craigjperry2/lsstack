# lsstack implementation plan

## 1. Purpose and authority

Build a concrete, runnable Python 3.12 starter application named `app` for
server-rendered Litestar applications. It is a starter to clone and edit, not a
Copier/Cookiecutter-style generator.

`README.md` describes the original architectural intent. This plan records the
decisions made after reviewing it and takes precedence where the two conflict.
The implementation must update `README.md` so that it describes only what the
repository actually provides.

The result is successful when a new developer on NixOS or nix-darwin can enter
the Nix shell, install locked Python dependencies, start the Compose
dependencies, migrate the database, run the web application and worker on the
host, and exercise the complete reference application through Nginx.

## 2. Scope

### In scope

- A concrete `src/app` package using Litestar, Jinja2, HTMX, Pico CSS,
  PostgreSQL, SQLAlchemy, Alembic, SAQ, Nginx, and a local LGTM observability
  stack.
- Registration, login, logout, a profile page with password change, and
  user-owned task CRUD.
- A generic transactional outbox and a demonstrative SAQ job triggered by task
  creation.
- Strict ports-and-adapters boundaries with a synchronous domain and
  application core.
- Local development on NixOS and nix-darwin, plus GitHub Actions verification.
- Security, architecture, cancellation, integration, and endpoint-performance
  tests.

### Explicit non-goals for this pass

- Kubernetes, Helm, Kustomize, staging, production deployment, TLS
  termination, container publishing, and GHCR.
- A Docker image for the Python application. Python runs on the host; Compose
  supplies dependencies.
- Email delivery, email verification, password recovery, account deletion, and
  breached-password network lookups.
- Browser automation and a Node/npm frontend toolchain.
- Dynamic HTML or HTMX-fragment caching in Nginx.
- A project generator or package-renaming machinery.
- A production observability-platform installation.

Remove or rewrite contrary claims in `README.md`, especially its current
Kubernetes, production startup, GHCR publishing, migration init-container, and
application-container language.

## 3. Fixed decisions

- Use Python 3.12, `uv`, `pyproject.toml`, `uv.lock`, a
  `src/app/__init__.py` layout, Ruff, basedpyright strict mode, pytest, Import
  Linter, and prek.
- Provide `flake.nix` and a committed `flake.lock`. The development shell must
  expose only Python 3.12 and `uv`; Python packages and developer tools such as
  Ruff, basedpyright, pytest, and prek belong in `pyproject.toml` and
  `uv.lock`. Do not duplicate them as Nix packages.
- Developers run the application on the host with Litestar's CLI. Use
  `litestar run`, which delegates to Uvicorn, rather than invoking Granian.
- Compose runs PostgreSQL 17, Nginx, `grafana/otel-lgtm`, and the collector
  needed to tail Nginx logs. It does not run the application or worker.
- Nginx proxies to the host application through `host.docker.internal`.
  Configure Compose's `host-gateway` mapping so the same name works on Linux;
  keep the native Docker Desktop behavior on macOS.
- Use the all-in-one, version-pinned `grafana/otel-lgtm` development image.
- Commit minified Pico CSS 2.1.1 and HTMX 2.0.10 beneath the application's
  static directory. Record their upstream URLs, versions, licenses, and
  checksums. Pages must make no CDN or other network requests at runtime.
- Accounts use normalized, case-insensitive email addresses as their unique
  login identifiers.
- Successful registration logs the new user in and redirects to their task
  list.
- Authentication cookies have a fixed 12-hour absolute lifetime. There is no
  sliding renewal or "remember me".
- Passwords are 12-128 Unicode characters. Reject leading/trailing whitespace
  and a password containing the normalized email address; do not add
  composition rules. Use Argon2id through `pwdlib`, including
  verify-and-rehash behavior.
- Password changes require the current password, increment a persisted session
  version, revoke every old cookie, and issue a replacement cookie only to the
  initiating browser.
- Nginx does not cache application HTML or HTMX fragments. Cache only local
  versioned static assets.
- Keep the domain and application core synchronous. Litestar handlers and SAQ
  tasks own async resource lifecycles and invoke one synchronous transaction
  callable through `AsyncSession.run_sync()`.
- Include SAQ with its PostgreSQL backend. A task-creation transaction writes a
  generic outbox message. An outbox relay publishes it to SAQ at least once.
- The example job sleeps asynchronously for 200 ms, then idempotently records
  `background_processed_at` on the task. The UI polls only pending task rows
  and shows the transition from processing to processed.
- Use one PostgreSQL database with two schemas and two login roles:
  `app_user` uses the `app` schema and `saq_user` uses the `saq` schema.
  Each role is denied privileges on the other schema.
- Use server-side HTTP integration tests rather than Playwright. Include a
  manual browser smoke checklist.
- CI uses `nix develop` on Ubuntu and macOS for the shared suite. Full
  PostgreSQL, Compose, Nginx, SAQ, performance, and LGTM checks run on Ubuntu.

## 4. Runtime topology

```text
Browser
  |
  v
Nginx container :8080
  |  proxy + request ID + security headers
  v
Litestar/Uvicorn on host :8000
  |                         \
  | async SQLAlchemy         \ OTLP logs/metrics/traces
  v                           v
PostgreSQL container       grafana/otel-lgtm container :3000
  |- app schema                ^
  |  |- users                  | OTLP
  |  |- tasks                  |
  |  `- outbox_messages        |
  `- saq schema                |
     |- saq_jobs               |
     |- saq_stats              |
     `- saq_versions           |
          ^                    |
          |                    |
SAQ worker on host ------------+
  |- periodic outbox relay
  `- process_task_created job

Nginx JSON log volume
  |
  v
OTel Collector Contrib container -> LGTM OTLP receiver
```

Bind published development ports to `127.0.0.1`, not all interfaces:

- Nginx: `8080`
- PostgreSQL: `5432`
- Grafana: `3000`
- OTLP gRPC/HTTP: `4317`/`4318`

Use named volumes for PostgreSQL and useful LGTM state, and a dedicated shared
volume for Nginx JSON logs. Add health checks and dependency conditions. Pin
container tags; never use `latest`.

## 5. Proposed repository layout

The coding agent may adjust individual filenames, but it must preserve these
module boundaries and responsibilities:

```text
.
├── .github/workflows/ci.yml
├── .importlinter
├── .pre-commit-config.yaml
├── alembic.ini
├── compose.yaml
├── flake.lock
├── flake.nix
├── pyproject.toml
├── uv.lock
├── README.md
├── PLAN.md
├── migrations/
│   ├── env.py
│   └── versions/
├── infra/
│   ├── nginx/
│   │   └── nginx.conf
│   ├── observability/
│   │   ├── nginx-otel-collector.yaml
│   │   └── grafana provisioning/dashboard files
│   └── postgres/
│       └── init.sql
├── src/app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── domain/
│   │   ├── users.py
│   │   ├── tasks.py
│   │   ├── outbox.py
│   │   └── errors.py
│   ├── application/
│   │   ├── ports/
│   │   ├── auth.py
│   │   ├── profiles.py
│   │   ├── tasks.py
│   │   └── outbox.py
│   ├── adapters/
│   │   ├── persistence/
│   │   ├── security/
│   │   ├── queue/
│   │   ├── observability/
│   │   └── web/
│   ├── templates/
│   │   ├── base.html
│   │   ├── auth/
│   │   ├── profile/
│   │   └── tasks/
│   └── static/
│       ├── app.css
│       └── vendor/
│           ├── pico-2.1.1.min.css
│           └── htmx-2.0.10.min.js
└── tests/
    ├── architecture/
    ├── unit/
    ├── integration/
    ├── performance/
    └── conftest.py
```

Keep framework and ORM objects out of the domain/application packages. Prefer
small, explicit modules over generic base classes or a speculative framework
within the starter.

## 6. Architecture contracts

### Domain and application

- Domain primitives are immutable frozen Pydantic models or frozen
  dataclasses.
- Application repository, unit-of-work, clock, password, public-ID, and outbox
  ports are synchronous typed `Protocol`s.
- Use cases accept ports through constructor or function parameters and return
  explicit result objects. Do not return ORM models, Litestar responses, or
  untyped dictionaries.
- Domain and application modules must contain no `async def`, `await`,
  `asyncio`, AnyIO, Litestar, SQLAlchemy, psycopg, SAQ, or OpenTelemetry
  imports.
- Define domain errors for expected outcomes such as duplicate email, invalid
  credentials, task not found, validation failure, and current-password
  mismatch. Translate them to HTTP behavior at the web adapter.

### Async transaction bridge

- Create a request-scoped `AsyncSession`; never share one across requests or
  concurrent tasks.
- Each handler validates edge input, then awaits a transaction runner that
  calls `AsyncSession.run_sync()`.
- The synchronous callable receives the proxied `Session`, constructs concrete
  repositories/unit of work, invokes one application use case, and returns a
  detached application result.
- The transaction runner owns commit/rollback and cancellation-safe cleanup.
- Only SQLAlchemy operations adapted by `run_sync()` may perform I/O inside
  that callable. Prohibit file, network, subprocess, sleep, or other blocking
  I/O there.
- Configure relationships for explicit eager loading. Do not allow accidental
  lazy loads in templates or after the transaction returns.
- The SAQ task uses the same transaction runner after its asynchronous sleep.

Enforce this with Import Linter, basedpyright, and AST tests rather than relying
on documentation alone.

### Concurrency and cancellation

- Ban `asyncio.create_task()` repository-wide.
- Use `asyncio.TaskGroup` or `anyio.create_task_group()` for owned concurrent
  work, including the event-loop-lag monitor lifecycle.
- Use async context managers for database sessions, queue pools, and exporters.
- Explicitly re-raise `asyncio.CancelledError` before handling broader
  `BaseException`; add the README's AST guard.
- Shield only short, truly non-cancellable operations such as final transaction
  commit, outbox acknowledgement, and lock/lease release. Put a timeout around
  shielded cleanup.
- Keep services request-scoped or immutable/stateless. Use `ContextVar` for
  request and trace correlation, never mutable process globals or thread-local
  state.

## 7. Data model and persistence

Use internal bigint primary keys and expose Sqids only at the web/public-ID
boundary. Never accept or render raw database IDs in task URLs.

### `app.users`

- `id`
- `email_normalized`, unique and indexed
- `password_hash`
- `session_version`, non-negative and defaulting to zero
- timezone-aware `created_at` and `updated_at`

Do not preserve a second ambiguous email representation unless it is explicitly
for display. Normalize consistently at registration and login.

### `app.tasks`

- `id`
- `user_id`, indexed foreign key with an explicit deletion policy
- `title` (trimmed, required, maximum 200 characters)
- optional `description` (maximum 5,000 characters)
- `is_completed`
- nullable `background_processed_at`
- timezone-aware `created_at` and `updated_at`

Every read/update/delete query must scope by both decoded task ID and current
user ID. Return not-found, rather than forbidden, for another user's or an
invalid public ID.

### `app.outbox_messages`

Create a reusable table, not a task-specific queue table:

- UUID `id`, used as the SAQ idempotency key
- `topic`
- versioned JSONB `payload`
- `created_at`
- nullable `published_at`
- nullable lease owner/expiry fields
- attempt count and nullable last error

Task creation and its `task.created.v1` outbox message must commit in one
application transaction.

### SAQ schema isolation

The PostgreSQL init script must:

- revoke public schema creation from `PUBLIC`;
- create `app` and `saq` schemas with distinct owners/login roles;
- set database-scoped role search paths to `pg_catalog, app` and
  `pg_catalog, saq` respectively;
- grant each role connect plus only the required privileges on its own schema;
- grant neither role privileges on the other schema.

The application DSN authenticates as `app_user`. The SAQ queue DSN
authenticates as `saq_user`. The worker process receives both because its queue
adapter talks to `saq` while its application use cases talk to `app`.

Alembic owns only the `app` schema and includes it explicitly in
autogeneration. SAQ owns and initializes its tables only in the `saq` schema.
Never copy SAQ's private schema into Alembic migrations.

Create an initial Alembic migration and verify upgrade from an empty database
and downgrade/upgrade round trips. Document the model-change/autogenerate/
review/edit/run/commit workflow from the original README.

## 8. Outbox and worker behavior

Provide one SAQ worker configuration through `litestar-saq` and the PostgreSQL
queue backend. Keep worker functions in the queue/infrastructure adapter.

The relay must provide at-least-once delivery:

1. Periodically claim a small batch of unpublished outbox rows in a short
   application transaction using `FOR UPDATE SKIP LOCKED` plus an expiring
   lease.
2. Commit the claims before crossing to SAQ.
3. Enqueue each message with the outbox UUID as the stable SAQ job key.
4. Acknowledge publication in a second short application transaction.
5. If enqueue or acknowledgement fails, record the attempt/error and allow the
   lease to expire for retry.
6. Treat "job with this key already exists" as successful publication. This
   closes the crash window between enqueue and acknowledgement.

Run the relay from the worker using a supported SAQ periodic/cron mechanism;
do not start an unowned background task. A stopped worker may delay delivery,
but cannot lose a committed message.

Register a versioned handler for `task.created.v1`. It must:

- validate the versioned payload;
- use `anyio.sleep(0.2)` (or the worker's equivalent non-blocking sleep);
- update `background_processed_at` only when it is still null;
- safely succeed if delivered more than once;
- use bounded retries with backoff and structured telemetry.

The request that creates a task must return without waiting for either relay or
job completion.

## 9. Web application

### Pages and flows

Implement at least:

- a root route that redirects authenticated users to tasks and anonymous users
  to login;
- registration;
- login;
- CSRF-protected logout;
- profile/password-change page;
- task list/create page;
- task edit, completion toggle, and delete actions;
- a task-row/status fragment used while background processing is pending;
- liveness and readiness endpoints with clearly different semantics.

Use ordinary form actions and server redirects as the baseline. Enhance task
operations with HTMX responses and fragments; do not create a parallel JSON
API. Detect HTMX requests at the edge and return either the relevant partial
or the full redirect/page. Avoid inline JavaScript.

The pending task row may poll its status at a modest interval (for example
250-500 ms). Once `background_processed_at` is present, render a row with no
polling attributes so polling stops automatically.

Render accessible semantic HTML compatible with Pico's class-light styling:
labels, validation summaries, focusable controls, disabled/loading states, and
useful empty states. Keep custom CSS small and local.

### Authentication and sessions

- Use Litestar encrypted client-side sessions with the cryptography extra.
- The encrypted cookie contains only the internal user ID, session version,
  issued-at time, and absolute expiry.
- Configure `HttpOnly`, `SameSite=Lax`, and a narrow path. Local development is
  plain HTTP, so its checked-in local configuration sets `Secure=false`;
  document that this is not a production configuration.
- Revalidate user existence, session version, and expiry on every authenticated
  request.
- Use a fixed 12-hour expiry and do not renew it during ordinary requests.
- Use generic login errors and constant-time password verification behavior
  that does not disclose whether an email exists.
- Registration atomically creates the user, logs them in, and redirects to
  tasks. Handle concurrent duplicate-email registration via the database
  unique constraint, not a check-then-insert race.
- Password change verifies the current password, validates/compares the new
  password and confirmation, replaces the hash, increments `session_version`,
  commits, and issues a new current-browser session containing the new version.
- On successful password verification, update an obsolete Argon2 hash using
  `pwdlib`'s verify-and-update flow.

### CSRF and headers

- Enable Litestar's signed double-submit CSRF protection for every unsafe
  method.
- Include `_csrf_token` in every unsafe HTML/HTMX form and test missing,
  malformed, and mismatched cases.
- Configure Nginx/app security headers, including a CSP that permits resources
  only from self, denies framing, and avoids inline script. Add
  `nosniff`, a conservative referrer policy, and appropriate permissions
  policy.
- Ensure Nginx forwards a validated/generate request ID and the application
  echoes it in the response and telemetry.

### Sqids

Implement the README's salt-specific deterministic alphabet permutation, then
construct the Sqids encoder with an explicit minimum length. Keep the salt and
alphabet in validated settings. Test stability for a fixed salt, changed output
for a changed salt, invalid input, and the rule that ownership is checked after
decoding.

## 10. Nginx and static assets

- Proxy dynamic traffic to `host.docker.internal:8000`.
- Do not configure a proxy cache for pages or fragments.
- Serve or proxy local static files with content-type safety and long-lived
  immutable cache headers. Use versioned asset filenames or content hashes so
  this caching is correct.
- Enable compression for suitable text assets.
- Set explicit request-body, header, connect, and response timeout limits that
  remain comfortable for local development.
- Emit structured JSON access and error logs containing request ID, method,
  path without sensitive query data, status, bytes, timing, upstream timing,
  user agent, and safe forwarding information.
- Never log cookies, authorization values, CSRF tokens, passwords, form bodies,
  or full query strings.
- Add a collector-contrib service with a `filelog` receiver over the shared
  Nginx log volume and an OTLP exporter to the LGTM container.

## 11. Observability

Instrument the host application to export OTLP over localhost to the Compose
LGTM receiver:

- structured JSON application logs;
- inbound HTTP spans and metrics;
- SQLAlchemy spans without bound parameter values;
- SAQ enqueue, relay, execution, retry, and failure telemetry;
- request ID, trace ID, and span ID correlation;
- the event-loop-lag gauge described in the README.

The lag monitor must measure drift from a scheduled 10 ms sleep, live for the
Litestar lifespan, and be owned by structured concurrency. Do not start it at
import time.

Provision a starter Grafana dashboard with request rate, errors, duration,
event-loop lag, queue activity, recent application/Nginx logs, and links
between traces and logs where supported. Provision the README's alert condition
for event-loop lag above 20 ms, with a short `for` duration to avoid a
single-sample alert.

Make telemetry initialization configurable and safe in tests:

- exporters are disabled or in-memory by default under pytest;
- application startup does not fail merely because LGTM is stopped;
- exporters shut down with bounded, cancellation-safe flushing;
- no telemetry field contains passwords, cookies, CSRF values, or task
  descriptions.

Add smoke checks that generate traffic through Nginx, then query the local
backends/Grafana data sources with bounded retries to prove that an application
trace/metric/log and an Nginx log arrived. Avoid fixed sleeps.

## 12. Configuration

Use one typed Pydantic settings object loaded explicitly at startup. At
minimum, validate:

- environment name and debug mode;
- app and admin/test database URLs;
- SAQ database URL;
- session and CSRF secrets;
- cookie security/lifetime settings;
- Sqids salt/alphabet/minimum length;
- public/proxy base URLs and trusted hosts;
- OTLP endpoint and telemetry enablement;
- log level and JSON/console mode.

Commit `.env.example` with local-only values and comments. Do not commit a
developer's `.env`. Fail fast with actionable messages for malformed database
URLs, weak/placeholder secrets outside the explicitly local environment, and
inconsistent cookie settings. Although deployment is out of scope, avoid
hard-coding settings inside adapters.

## 13. Nix, dependencies, and developer commands

The flake must support the team's common combinations:

- `x86_64-linux`
- `aarch64-linux`
- `x86_64-darwin`
- `aarch64-darwin`

Its default dev shell exposes Python 3.12 and `uv`, and sets no global Python
package path. Let `uv` own the virtual environment.

Organize `pyproject.toml` dependency groups for runtime, test, quality, and
observability needs. Pin through `uv.lock`, not loose ad hoc install commands.
Include the appropriate extras for Litestar CLI/cryptography, SQLAlchemy
asyncio/greenlet, psycopg async/pool, SAQ PostgreSQL, Argon2, templates, and
OpenTelemetry. Verify wheels in both Linux and Darwin CI.

Document this local sequence:

```console
nix develop
uv sync --all-groups --frozen
docker compose up -d --wait
uv run alembic upgrade head
uv run litestar run --reload --host 0.0.0.0 --port 8000
```

In a second terminal, start the SAQ worker through the command exposed by
`litestar-saq` (normally `uv run litestar workers run`). Configure
`LITESTAR_APP=app.main:app` so commands stay concise. Then browse the
application through `http://localhost:8080`, not directly through port 8000,
and Grafana through `http://localhost:3000`.

Also document database reset, worker startup, migrations, tests, linting,
formatting, type checking, hooks, logs, and clean Compose shutdown. Do not hide
the essential commands behind a new task runner in this pass.

## 14. Test strategy

### Database safety fixture

Preserve and extend the README's safety requirements:

- require the disposable database name to end in `_test`;
- connect through a separate admin URL capable of create/drop;
- create a fresh database plus `app`/`saq` schemas and their grants;
- run the full Alembic chain and let SAQ initialize its own schema;
- truncate application, outbox, and SAQ state between tests without weakening
  schema isolation;
- close every engine/pool before dropping the database;
- refuse destructive setup/teardown if any safety check fails.

### Unit tests

- Pure domain invariants and immutable value behavior.
- Email and password validation.
- Authentication, profile, task, and outbox use cases with typed fakes.
- Session expiry/version decisions.
- Sqids stability and invalid decoding.
- Idempotent task-job processing.

### Integration tests

- Migration from empty database and schema/role isolation.
- Registration auto-login, duplicate-email race handling, login/logout, fixed
  session expiry, tampered cookies, and generic failures.
- Password change, old-session revocation, new current session, wrong current
  password, and hash upgrade.
- CSRF on every unsafe route for full-page and HTMX requests.
- All task CRUD paths, public IDs, ownership isolation, validation, fragments,
  redirects, and not-found behavior.
- Atomic task/outbox commit and rollback.
- Relay retry after enqueue failure, duplicate enqueue after crash, lease
  expiry, and eventual acknowledgement.
- SAQ job retry/idempotency and the UI's pending-to-processed transition.
- Nginx routing, headers, static caching, no dynamic cache, request IDs, and
  log redaction.
- Telemetry correlation and LGTM ingestion smoke tests.

### Performance tests

Mark performance tests separately but run them in Linux CI. Warm the
application and database first, use the in-process async HTTP client to exclude
Nginx/network noise, measure with a monotonic high-resolution clock, and assert
that representative registration/login/task CRUD/HTMX status handlers finish
within 100 ms under the controlled test workload. The task-create assertion
must stop at the committed outbox write; separately prove that the 200 ms sleep
occurs in the worker.

Do not weaken the threshold to accommodate an accidental inline job or
blocking call. Avoid a single cold-start sample as the pass/fail signal.

### Architecture tests

Add AST/import tests that:

- reject async functions and forbidden imports in domain/application;
- reject `asyncio.create_task`;
- reject `BaseException` handlers that do not explicitly preserve
  cancellation;
- reject mutable domain models;
- enforce layer contracts through Import Linter;
- detect obvious process-global mutable request state;
- keep ORM/framework objects from crossing application results.

### Manual browser smoke test

Document a short checklist: register, create/edit/toggle/delete tasks, observe
the queued status change without a full reload, change password, verify another
old session is rejected, inspect responsive Pico styling, and follow one
request across Grafana logs/traces/metrics.

## 15. CI

Create GitHub Actions jobs with least privilege and dependency caching:

1. A Linux and macOS matrix installs Nix, evaluates `flake.lock`, enters
   `nix develop`, runs `uv sync --all-groups --frozen`, then runs formatting,
   Ruff, basedpyright, Import Linter, architecture tests, and database-free
   unit tests.
2. An Ubuntu integration job starts the Compose stack, runs migrations and the
   host app/worker as supervised CI processes, then runs database integration,
   endpoint-performance, Nginx, queue/outbox, and observability smoke tests.
3. Always collect useful service/application logs on failure and always stop
   processes/Compose.

Do not build or publish an application container. Pin action revisions and use
timeouts/concurrency cancellation so failed jobs cannot hang indefinitely.

## 16. Implementation order

Each phase must leave its own tests passing. Do not scaffold every layer first
and postpone integration until the end.

1. **Foundation:** create the Nix flake/lock, `pyproject.toml`, `uv.lock`,
   source layout, settings, quality tools, prek hooks, Import Linter contracts,
   and basic CI matrix.
2. **Local services:** add PostgreSQL schemas/roles, Compose health checks,
   Nginx host proxying, LGTM, and the Nginx log collector. Prove portability
   with `docker compose config` and basic health smoke tests.
3. **Persistence boundary:** add SQLAlchemy models, Alembic, the disposable
   database fixture, synchronous repository protocols/adapters, and the
   `AsyncSession.run_sync()` transaction runner. Test rollback and
   cancellation.
4. **Authentication:** implement users, registration auto-login, encrypted
   fixed-expiry sessions, Argon2id, logout, CSRF, and security headers with
   full integration coverage.
5. **Task vertical slice:** implement owned task CRUD, Sqids URLs, full pages,
   HTMX fragments, vendored assets, accessible Pico-based templates, and
   endpoint performance tests.
6. **Outbox and worker:** add the generic outbox migration/use case, isolated
   SAQ schema/role, relay leases/idempotent enqueue, 200 ms example job, polling
   status fragment, and crash/retry tests.
7. **Profile:** add current-password verification, password replacement,
   session-version increment, current-cookie reissue, and multi-session tests.
8. **Observability:** instrument application/database/worker, add structured
   correlated logs, event-loop lag, dashboards/alert, Nginx log shipping, and
   bounded ingestion smoke tests.
9. **Hardening and handoff:** run the complete verification matrix, inspect
   rendered pages manually, remove stale claims and dead code, rewrite the
   README for the local-only workflow, and ensure a clean checkout can follow
   it exactly.

## 17. Completion criteria

The coding agent must not declare completion until all of the following hold:

- `nix flake check` succeeds on supported systems in CI.
- `uv sync --all-groups --frozen` succeeds from the committed lock.
- Ruff lint and format checks, basedpyright strict mode, Import Linter, prek,
  and all pytest suites pass.
- `docker compose config --quiet` and all container health checks pass.
- The app is reachable through Nginx while Python and the worker run on the
  host.
- A clean database migrates successfully; the app and SAQ roles cannot access
  each other's schemas.
- Registration/login/logout/password change and all owned task CRUD flows work
  with CSRF and encrypted sessions.
- Creating a task atomically writes an outbox message, returns within the
  endpoint budget, and eventually shows the worker's persisted processed
  timestamp after the non-blocking 200 ms job.
- Relay crashes/duplicates cannot lose a committed message or apply the task
  effect twice.
- Dynamic responses are not cached; local static assets are cached and the
  browser makes no CDN requests.
- Application telemetry and Nginx logs are visible and correlated in the local
  Grafana stack, and the event-loop-lag alert is provisioned.
- `README.md` contains reproducible setup, run, migration, worker, verification,
  troubleshooting, and manual-smoke instructions and makes no unsupported
  production/deployment claims.

## 18. Primary implementation references

Prefer current stable APIs and verify them while implementing:

- Litestar Docker/CLI guidance:
  <https://docs.litestar.dev/2/topics/deployment/docker.html>
- SQLAlchemy `AsyncSession.run_sync()`:
  <https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#sqlalchemy.ext.asyncio.AsyncSession.run_sync>
- Pico CSS 2 documentation:
  <https://picocss.com/docs>
- HTMX installation/documentation:
  <https://htmx.org/docs/#installing>
- SAQ documentation:
  <https://saq-py.readthedocs.io/en/stable/>
- SAQ PostgreSQL implementation and table configuration:
  <https://github.com/tobymao/saq/blob/v0.26.4/saq/queue/postgres.py>
- Grafana Docker OpenTelemetry LGTM:
  <https://github.com/grafana/docker-otel-lgtm>

If a stable dependency's API has moved, adapt the implementation and lock the
working version; do not silently change the architectural decisions in this
plan.
