# Deviations from the initial bootstrap plan

This file records conservative implementation choices made only when the plan
cannot be followed literally because of an edge case or an upstream constraint.

## SAQ handler key for the versioned task-created topic

- **Plan intent:** register a versioned `task.created.v1` handler and use the
  outbox UUID as the stable SAQ idempotency key.
- **Edge case:** `litestar-saq` 0.8.0 accepts a `(name, function)` task tuple,
  but its configuration normalization retains only the function. SAQ therefore
  derives the callable key from the Python function name rather than preserving
  `task.created.v1` as the callable key.
- **Conservative choice:** the durable outbox topic and payload remain
  `task.created.v1` and version 1. The relay explicitly maps that topic to the
  registered `process_task_created_job` callable, while the SAQ job key remains
  the outbox UUID. The implementation does not mutate function metadata or
  private plugin state.

## Nginx native error-log handling

- **Plan intent:** retain useful Nginx runtime errors while shipping structured
  logs without full query strings or sensitive request data.
- **Edge case:** the pinned stock Nginx image supports a custom format for
  access logs, but not for its native error log. Native error lines can contain
  the complete request target, including its query string. Correcting this
  would require a custom image or additional scripting/module support outside
  the planned local stack.
- **Conservative choice:** write the native error log to container stderr at
  `warn` severity, where it is available with `docker compose logs nginx`, but
  do not add it to the filelog collector. Only the structured JSON access log,
  whose `path` uses query-free `$uri`, is shipped to LGTM. Native error lines
  are a local, non-JSON diagnostic stream and can contain the full request
  target, so developers must not put secrets in query strings.

## SAQ role search-path order

- **Plan intent:** set the SAQ login role's database-scoped search path to
  `pg_catalog, saq`, with no rights on the application schema.
- **Edge case:** SAQ 0.26.4 creates its own tables without schema qualification.
  PostgreSQL selects the first search-path entry for an unqualified `CREATE
  TABLE`, so the planned order attempts to create `saq_versions` in
  `pg_catalog` and fails with `permission denied for schema pg_catalog`.
  SAQ's table-name options quote their input as one identifier and therefore
  cannot safely express a schema-qualified table name.
- **Conservative choice:** set only `saq_user` to `saq, pg_catalog`. The role
  still has no privileges on `app` or `public`; this changes creation lookup
  order without weakening cross-schema isolation. `app_user` retains the
  planned `pg_catalog, app` order because Alembic and SQLAlchemy explicitly
  qualify application objects.

## Disposable database reset granularity

- **Plan intent:** create a fresh disposable test database and truncate
  application, outbox, and SAQ state between integration tests.
- **Edge case:** truncating SAQ's private tables from the application test
  harness would require the harness to retain or assume SAQ-internal table
  knowledge and carefully coordinate live queue connections between tests.
- **Conservative choice:** the function-scoped integration fixture drops and
  recreates the `_test` database, both isolated schemas, the full Alembic chain,
  and SAQ-owned tables for every database test. This is slower than truncation
  but provides stronger isolation, preserves the two-role privilege boundary,
  and avoids coupling cleanup to SAQ's private schema.

## SAQ web-process heartbeat lifecycle

- **Plan intent:** expose one `litestar-saq` worker configuration while the web
  server and worker run as separate host processes.
- **Edge case:** `litestar-saq` 0.8.0 registers its heartbeat manager on every
  Litestar application startup even when `use_server_lifespan=false` and the
  worker is explicitly configured for a separate process. The web server
  therefore opens an unused queue heartbeat and its upstream synchronous stop
  path can outlive the event loop during shutdown.
- **Conservative choice:** retain the exact `SAQPlugin` type required by the
  plugin's CLI registry, eagerly obtain its configured worker, and disable only
  that worker's private `_enable_heartbeat_manager` compatibility switch. The
  separate-process startup/shutdown hooks otherwise remain intact and are
  no-ops in the web process. `litestar workers run` obtains and runs the same
  configured worker without giving the web process an unused SAQ lifecycle;
  its two registered jobs are short and bounded and do not use the optional
  `monitored_job` heartbeat decorator.
  Configure the supported SAQ `worker_info` and `sweep` upkeep timers to four
  seconds, below the five-second graceful-shutdown window; the upstream defaults
  sleep for 10 and 60 seconds and therefore force an otherwise-idle worker to
  warn and cancel those pollers on every normal shutdown.
