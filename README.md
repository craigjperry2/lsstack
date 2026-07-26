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
`aarch64-darwin`. It exposes Python 3.12, `uv`, `just`, Git, GitHub CLI,
Node.js, and `pnpm`; `uv` owns `.venv` and all Python developer tools.

## First run

Start the entire local stack from a clean checkout with one command:

```console
nix develop --command just up
```

`just up` creates `.env` from `.env.example` when needed, installs the exact
locked dependencies, allocates an isolated Compose project and ports for the
current worktree, waits for the containers, migrates the database, starts the
host application and worker, and waits for application readiness through
Nginx. Repeating it is safe: already-running owned processes are reused.

Enter the shell and list every supported command when working interactively:

```console
nix develop
just --list
```

The startup output prints this worktree's application and Grafana URLs. The
primary checkout prefers the conventional ports below; linked worktrees use
persisted collision-resistant alternatives so multiple stacks can run in
parallel:

- application through Nginx: [http://localhost:8080](http://localhost:8080)
- Grafana: [http://localhost:3000](http://localhost:3000)

Use `just urls` rather than assuming these ports. Do not browse directly to the
host application port: going through Nginx exercises request-ID propagation,
security headers, static caching, and access-log collection. The LGTM image is
a development observability backend, not a production monitoring installation.

The checked-in secrets and passwords are deliberately local-only. Session
cookies use `Secure=false` because local Nginx serves plain HTTP. Do not reuse
these settings outside an isolated development machine.

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

Discover recipes and inspect the running stack:

```console
just --list
just status
just urls
just diagnose
```

Run all tests or focused subsets:

```console
just test-fast
just test-unit
just test-architecture
just test-integration
just test-performance
just test-service
just test-all
just test tests/unit/test_config.py -k cookie
```

`test-integration`, `test-performance`, `test-service`, and `test-all` start the
worktree stack as needed and export its allocated URLs. The database fixture
still refuses destructive setup unless `TEST_DATABASE_URL` names a disposable
database ending in `_test`. It uses `ADMIN_DATABASE_URL` only to create and
drop that database. Never point these variables at shared data.

Run the non-mutating quality gate, apply formatting fixes, or exercise the
configured hooks:

```console
just check
just fix
just hooks-install
just prek
```

Seed the documented local demo account and deterministic sample tasks:

```console
just db-seed
```

The defaults are `agent@example.test` and `correct-horse-battery`, configurable
through `DEV_SEED_EMAIL` and `DEV_SEED_PASSWORD`. Seeding is idempotent and
refuses non-local environments or non-loopback databases.

Stop the current worktree's services without deleting their data:

```console
just down
```

To reset only the current worktree's PostgreSQL and LGTM volumes, first ask for
the project-specific confirmation token:

```console
just db-reset
# Then repeat the command with the printed reset-<project> token.
```

## Database changes

After changing a SQLAlchemy model, generate a candidate migration:

```console
just db-revision "describe the change"
```

Review and edit the generated migration before running it. In particular,
check schema names, deletion policies, constraints, indexes, data movement,
and whether an apparent drop/add should instead be a rename. Then verify both
directions against a disposable local database:

```console
just db-migrate
uv run alembic downgrade -1
just db-migrate
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

Agents can query the same backends directly through the active worktree's LGTM
container:

```console
just obs logs --since 15m
just obs traces --query '{ resource.service.name = "lsstack" && duration > 100ms }'
just obs metrics --query 'event_loop_lag_seconds'
just obs correlate <request-id>
just obs --json traces
just obs check
```

The commands return concise output by default and JSON on request. Empty
results explain the likely exporter-ingestion delay and can be retried without
changing state.

The Nginx access log contains the request ID, method, path without its query
string, response status and size, timings, user agent, and safe forwarding
information. It never records cookies, authorization, form bodies, passwords,
CSRF values, or full query strings. Only this structured JSON access log is
shipped to LGTM. Native runtime errors are written to container stderr at
`warn` severity and are available with `just logs nginx`; they are
not added to the filelog collector. Stock Nginx error lines are not JSON and
can contain the full request target. This is a local diagnostic stream, so do
not put secrets in query strings. See the
[initial bootstrap deviations](docs/plans/initial-bootstrap-deviations.md) for
the conservative security decision.

## Troubleshooting

If `just up` fails, start with `just diagnose`. It includes Compose state plus
bounded app, worker, and container logs. Use `just logs app`, `just logs worker`,
or `just logs <compose-service> --follow` for a live stream. PostgreSQL
initialization scripts run only when its data volume is empty; after changing
local roles or the init script, reset this worktree's volumes as described
above.

If Nginx returns `502 Bad Gateway`, use `just status` to confirm that Litestar
is listening on this worktree's allocated app port and that Docker supports
the configured host-gateway mapping.
Inspect `just logs nginx` for the native upstream connection error.
Only the structured access log is shipped to LGTM; native error lines remain
on container stderr, are not JSON, and can contain the full request target, so
do not put secrets in query strings.
The `/nginx-health` endpoint proves only that Nginx itself is running; `/livez`
and `/readyz` are application liveness and dependency-readiness checks.

If login works with an HTTP client but not a browser, confirm `.env` still has
`SESSION_COOKIE_SECURE=false` for local HTTP and that you are browsing the
application URL from `just urls`. Clear old cookies for that host after
changing session or CSRF secrets.

If telemetry is absent, confirm `TELEMETRY_ENABLED=true`, check the application,
worker, collector, and LGTM logs, then generate traffic through Nginx. Exporters
retry with bounded queues, so LGTM can start after the host processes.

## Manual browser smoke check

Install the pinned agent-friendly Playwright CLI with `pnpm` and keep each flow
in an ignored artifact directory:

```console
just browser-install
just urls
just browser smoke open <application-url-printed-above>
just browser smoke snapshot
just browser smoke requests
just browser smoke request 1
just browser smoke console warning
just browser smoke screenshot
```

The repository never uses `npx`; browser commands run pinned
`@playwright/cli@0.1.17` through `pnpm dlx`. You can also use
`tracing-start`/`tracing-stop`. Artifacts are stored in
`output/playwright/<session>/`.

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
