# Initial bootstrap review action plan

## 1. Purpose and authority

This plan resolves the four findings raised against the initial bootstrap
implementation:

1. credential updates are not serialized;
2. Nginx discards useful runtime errors;
3. pytest collection can start real OTLP exporters;
4. the database endpoint budget substitutes a fake password hasher.

Use this plan together with
[`initial-bootstrap.md`](initial-bootstrap.md) and
[`initial-bootstrap-deviations.md`](initial-bootstrap-deviations.md). Where this
plan is more specific, it takes precedence for the review fixes. Do not broaden
the work into unrelated refactoring.

The work is complete only when the concurrency guarantees are demonstrated
against PostgreSQL, pytest cannot initialize configured OTLP exporters during
collection, Nginx upstream failures remain visible in container logs, and the
100 ms database endpoint budget exercises the application's configured Argon2
adapter.

## 2. Fixed implementation decisions

- Serialize password changes and login-time hash upgrades with a PostgreSQL row
  lock on the affected `app.users` row. Do not use an in-process lock; it would
  not coordinate multiple web processes.
- Acquire the user lock before verifying a password and hold it until the
  request transaction commits or rolls back. This deliberately serializes
  credential verification and mutation for one account while allowing
  unrelated accounts to proceed independently.
- Keep ordinary per-request session revalidation on non-locking reads. Only
  credential-sensitive operations need the row lock.
- Replace the broad detached-user update operation with a
  credential-specific update that changes only `password_hash`,
  `session_version`, and `updated_at`. It must not rewrite the normalized email
  from a stale detached value.
- Disable telemetry in the pytest process before test modules are collected.
  Tests that exercise telemetry internals must use explicit settings and
  fakes/in-memory test doubles; they must not contact the local LGTM endpoint.
- Keep application and worker telemetry enabled in the separately launched CI
  processes so the existing observability ingestion smoke test remains
  meaningful.
- Send Nginx native errors to container stderr at `warn` severity. Continue to
  ship only the query-free structured access-log file to LGTM.
- Keep the endpoint threshold at a median below 100 ms. Do not satisfy the test
  by substituting a fast hasher, excluding hashing from the timed action, or
  raising the threshold.

No database migration is required.

## 3. Credential and session-version serialization

### 3.1 Make locking explicit in the application port

Update `src/app/application/ports/persistence.py`:

- Add explicit lock-taking reads such as `get_by_id_for_update()` and
  `get_by_email_for_update()` to `UserRepository`.
- Retain `get_by_id()` for session revalidation and other read-only work.
- Retain a non-locking email lookup only if a current caller still needs it;
  authentication itself must use the lock-taking lookup.
- Replace the generic `update(User)` contract with an intent-specific
  credential update, for example `update_credentials(User)`.
- Document that the `for_update` methods lock the row until the surrounding
  unit-of-work transaction ends and that `update_credentials()` may only be
  used after one of those reads.

Update every fake implementation in `tests/conftest.py` to satisfy the revised
protocol. The fake lock-taking methods can delegate to their ordinary lookup,
but their separate names should remain so unit tests can prove that the
credential use cases select the locking path.

### 3.2 Implement PostgreSQL row locking and narrow writes

Update `src/app/adapters/persistence/repositories.py`:

- Implement both lock-taking reads with `select(UserModel)`,
  the relevant ID or normalized-email predicate, and
  `.with_for_update()`. Do not use `skip_locked` or `nowait`: a competing
  credential request must wait for the current credential transaction.
- Keep the lock scoped to the existing request-owned SQLAlchemy session and
  transaction. Do not add a commit inside the repository.
- Make the credential update write only the password hash, session version,
  and update timestamp, then flush and return fresh detached state.
- Preserve the missing/unpersisted-user checks.
- Remove the old broad update method once all callers have moved. A detached
  credential update must never write `email_normalized`.

### 3.3 Use the locked state in both mutation paths

Update `src/app/application/auth.py`:

- In `authenticate()`, look up an existing account with
  `get_by_email_for_update()` before calling `verify_and_update`.
- If `pwdlib` returns a replacement hash, persist it with the narrowed
  credential update.
- Build `AuthResult` from the locked, current user state. Never issue a cookie
  using the session version from an earlier, unlocked snapshot.
- Preserve the unknown-account dummy verification and generic login error.

Update `src/app/application/profiles.py`:

- In `change_password()`, fetch the account with `get_by_id_for_update()` before
  verifying the current password.
- Validate and hash the new password while the row remains locked, increment
  the locked row's session version exactly once, and persist through the
  narrowed credential update.
- Return the committed transaction's new session version to the handler so only
  the successful initiating request can receive the replacement cookie.

The desired concurrent behavior is:

- If two password changes begin with the same cookie and current password, the
  first committed change succeeds. The second request then observes the new
  hash under the lock, fails current-password verification, issues no
  replacement cookie, and cannot collapse the version back to the first
  request's value.
- If a login-time hash upgrade wins the lock first, it may update only the hash
  without changing `session_version`; a following password change then uses
  that current state.
- If a password change wins first, a login using the old password must observe
  the new hash after waiting, fail authentication, and issue no stale-version
  cookie.

### 3.4 Add deterministic PostgreSQL concurrency regressions

Add a focused module such as
`tests/integration/test_postgres_credential_concurrency.py`. Use the disposable
database harness and two independent sessions/transactions; never share an
`AsyncSession` between concurrent tasks.

Cover at least:

1. Two password changes starting from session version 0 and the same current
   password. Drive this case through two HTTP clients that share the original
   encrypted session and CSRF cookies. Assert exactly one response succeeds and
   receives a version-1 replacement cookie, the other reports a
   current-password mismatch without receiving a replacement cookie, the final
   version is 1, only the winner's new password verifies, and the original
   version-0 cookie is invalid.
2. A login-time hash upgrade racing a password change. Exercise both lock
   orderings. Assert the final password is always the explicitly changed
   password, the final session version is 1, the obsolete/rehashed old
   credential is never restored, and a login that loses to the password change
   cannot return a version-0 `AuthResult`.
3. An uncontended obsolete-hash login still replaces the hash without
   incrementing the session version.

Coordinate transactions with events/barriers and bounded timeouts so the
dangerous ordering is deliberate. Do not use fixed sleeps as proof of
concurrency. It is acceptable for the test to hold the first transaction open
with the async SQLAlchemy session API, then run the same repository/use-case
code through `run_sync()` in each transaction. Ensure every task and session is
closed on assertion failure.

Keep the existing unit and web-flow assertions for ordinary password changes,
hash upgrades, cookie replacement, and old-cookie revocation. Extend the unit
tests to assert that `authenticate()` and `change_password()` call the locking
repository methods rather than their non-locking counterparts.

## 4. Preserve Nginx runtime diagnostics

Update `infra/nginx/nginx.conf`:

- Change the native error log target from `/dev/null` to `/dev/stderr`.
- Use the `warn` threshold so ordinary upstream connection failures, including
  the failure behind a `502 Bad Gateway`, are retained:

  ```nginx
  error_log /dev/stderr warn;
  ```

- Do not add the native error stream to the filelog collector. The structured
  access log remains the only Nginx log shipped to LGTM.

Update the Nginx section of
`docs/plans/initial-bootstrap-deviations.md` and the Observability and
Troubleshooting sections of `README.md`:

- State that native runtime errors are written to container stderr and are
  available with `docker compose logs nginx`.
- State that only the structured access log is shipped to LGTM.
- Preserve the warning that stock Nginx error lines are not JSON and can
  contain the full request target. This is a local diagnostic stream, so
  developers should not put secrets in query strings.
- Remove the inaccurate claim that only startup/container-process diagnostics
  are available.

Verification must include `nginx -t` in the container and a controlled request
while the host application is unavailable. Assert that Nginx returns 502 and
that `docker compose logs nginx` contains an upstream connection error. Avoid
including a sensitive query value in this probe. This can be a bounded CI smoke
step near the end of the integration job or a documented reproducible smoke
command if process control makes an automated test unsafe.

## 5. Disable OTLP exporters before pytest collection

Update `tests/conftest.py`:

- Set `TELEMETRY_ENABLED=false` at module import time, before importing any
  `app` module. This must be test bootstrap code, not an autouse fixture or
  lifespan hook; both are too late to protect collection imports.
- Override a developer shell's or copied `.env` value deliberately. Pytest must
  remain isolated even when `.env` contains `TELEMETRY_ENABLED=true`.
- Add a short comment explaining the collection-order requirement.

Do not change `.env.example` to disable normal local observability, and do not
detect pytest from production application code.

Extend `tests/unit/test_observability.py` and/or `tests/unit/test_config.py` to
prove:

- loading the checked-in environment example inside pytest still resolves
  telemetry as disabled because the test-process environment wins;
- importing `app.main` leaves the module-level application's telemetry runtime
  disabled and creates no OTLP exporter/provider threads;
- explicitly constructed disabled telemetry remains inert;
- telemetry-specific unit tests use recording/in-memory objects rather than a
  network exporter.

If an exporter-construction assertion needs module reloading, patch the three
OTLP exporter constructors before reloading and make them fail the test if
called. Restore global modules, logger handlers, and OpenTelemetry state after
the test so test order does not matter.

The separately launched Litestar and worker processes in
`.github/workflows/ci.yml` must continue to receive
`TELEMETRY_ENABLED=true`. Only the pytest subprocess is forced off; the existing
post-test LGTM ingestion check still validates real exporters.

## 6. Measure the configured Argon2 endpoint path

Update `tests/performance/test_database_endpoint_budget.py`:

- Remove the `FastPasswordHasher` import, the `dataclasses.replace` import, and
  the replacement of `application.state.web_dependencies.passwords`.
- Use the `PwdlibPasswordHasher` installed by `create_app()` for registration
  and login.
- Keep application construction, client startup, CSRF acquisition, and
  deliberate warm-up outside the timed samples.
- Time the complete HTTP action, including Argon2 hashing during registration
  and Argon2 verification during login.
- Continue to use at least ten samples and the median-below-100-ms assertion.

Also cover the configured verify-and-update branch:

- Before timing, seed at least ten distinct users with valid but obsolete
  Argon2id hashes made by a lower-cost/legacy `pwdlib` Argon2 configuration.
  Seed setup is not part of the endpoint timing.
- Log into each seeded account once through `/login` inside the timed samples,
  so every measured request performs verification and produces the configured
  replacement hash.
- After each request, or in one untimed verification query after the samples,
  assert that the persisted hash changed and is accepted without another
  replacement by the application's configured hasher.

Do not reuse one upgraded account for all ten samples, because only its first
login would exercise verify-and-update. Do not move the replacement-hash
database write outside the endpoint action.

Keep task CRUD timing on the same real application and database. If the real
Argon2 path cannot meet the fixed budget in the controlled Linux CI
environment, investigate application configuration and measurement noise.
Do not restore a fake hasher or silently weaken the threshold; report a genuine
requirements conflict explicitly.

## 7. Documentation and verification order

Implement and verify in this order so failures remain attributable:

1. Add the repository locking API, narrow the credential update, update the
   application use cases and fakes, then run database-free unit/type checks.
2. Add and pass the PostgreSQL concurrency regressions.
3. Add the pre-collection telemetry override and isolation tests.
4. Restore Nginx stderr diagnostics and update the recorded deviation and
   README.
5. Replace the fake performance path with configured Argon2, add legacy-hash
   samples, and run the controlled database performance test.
6. Run the full repository verification and inspect the diff for unrelated
   changes.

Suggested commands, using the repository's documented Nix/uv environment:

```console
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
uv run lint-imports
uv run pytest tests/architecture tests/unit
docker compose config --quiet
docker compose up -d --wait
set -a
. ./.env
set +a
RUN_DATABASE_TESTS=1 uv run pytest \
  tests/integration/test_postgres_credential_concurrency.py \
  tests/integration/test_postgres_web_flows.py \
  tests/performance/test_database_endpoint_budget.py
docker compose exec -T nginx nginx -t
RUN_DATABASE_TESTS=1 RUN_SERVICE_TESTS=1 uv run pytest \
  tests/integration tests/performance
```

Use the existing disposable-database safety guard; never point
`TEST_DATABASE_URL` at shared data. Stop Compose normally after verification
without deleting developer volumes.

## 8. Acceptance checklist

- Concurrent password changes cannot both succeed from the same credential
  snapshot, cannot issue two valid replacement cookies at the same version,
  and leave the persisted session version monotonic.
- A concurrent login hash upgrade cannot overwrite a newer password or restore
  an older session version.
- Credential writes no longer rewrite unrelated user fields from detached
  state.
- Nginx upstream failures appear in `docker compose logs nginx` at the normal
  warning/error levels, while only sanitized access JSON is shipped to LGTM.
- Importing `app.main` during pytest collection cannot construct real OTLP
  exporters even when the developer `.env` enables telemetry.
- The separately launched app and worker still export telemetry in the
  observability smoke environment.
- The database registration/login budget uses the configured Argon2 adapter,
  includes verify-and-update samples, and retains the median 100 ms threshold.
- Ruff, formatting, basedpyright, Import Linter, architecture, unit,
  integration, performance, Nginx, and observability checks all pass.
