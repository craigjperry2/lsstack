# Repository guidance for coding agents

## Start here

- Work from an isolated Git worktree on a `codex/<short-task>` feature branch.
  Do not develop directly on `main`.
- Codex-managed worktrees may copy the ignored local `.env` because it is
  listed in `.worktreeinclude`; still inspect it and keep it untracked.
- Enter the pinned toolchain with `nix develop`. From outside it, any recipe can
  be run as `nix develop --command just <recipe>`.
- Run `just --list` for the authoritative command inventory.
- `just up` is the clean-worktree entry point. It creates `.env` when absent,
  performs a frozen dependency sync, starts this worktree's isolated Compose
  project, migrates the database, starts the app and worker, waits for
  readiness, and prints the allocated URLs.
- Use `just status`, `just urls`, `just diagnose`, and `just logs --help` before
  manually reproducing any startup step. Use `just down` when finished;
  normal shutdown preserves this worktree's volumes.

## Architecture and safety boundaries

- Keep the domain and application layers independent of Litestar, SQLAlchemy,
  SAQ, and other infrastructure. I/O belongs in adapters and composition roots;
  `just test-architecture` enforces the import contracts.
- Run synchronous repository work only through `TransactionRunner`. Preserve
  owner-scoped task access, encrypted fixed-lifetime sessions, CSRF protection,
  and the transactional outbox/SAQ idempotency guarantees.
- Alembic owns only the `app` schema. Never add SAQ's private `saq` tables to
  migrations.
- After model changes, use `just db-revision "description"`, review the
  generated migration for schemas, constraints, indexes, data movement, and
  accidental drop/add operations, then test upgrade/downgrade/upgrade.
- Database-backed tests may destroy only a database whose name ends in `_test`.
  Never point `TEST_DATABASE_URL` or `ADMIN_DATABASE_URL` at shared data.
- `just db-reset` is destructive only to the current worktree's isolated
  Compose volumes and requires the exact confirmation token it prints.
- `.env` contains local-only credentials. Never commit it or reuse its values
  outside an isolated development machine.

## Tests and quality

- Use `just test-fast` for the normal edit loop.
- Use `just test-unit`, `just test-architecture`, `just test-integration`,
  `just test-performance`, or `just test-service` for focused verification.
- `just test <pytest arguments...>` passes arbitrary paths and selectors to
  pytest; for example, `just test tests/unit/test_config.py -k cookie`.
- `just test-all` starts the stack and opts into every database and service
  test. Run it before a pull request when the environment supports Docker.
- `just check` is the non-mutating full local quality gate. `just fix` changes
  formatting and applies safe Ruff fixes. `just prek` exercises both configured
  hook stages, and `just hooks-install` installs them.
- Add or update tests with behavior changes. Do not weaken assertions, skip
  safety checks, or increase performance budgets merely to make a failure pass.

## Debugging and observability

- Browse through the Nginx URL printed by `just urls`, never directly through
  the host app port. This exercises request IDs, security headers, caching, and
  structured Nginx access logging.
- Read host logs with `just logs app` or `just logs worker`. Read containers
  with `just logs postgres`, `just logs nginx`, `just logs lgtm`, or
  `just logs nginx-log-collector`; add `--follow`, `--tail`, or `--since`.
- Query telemetry without scraping the Grafana UI:
  - `just obs logs --since 15m`
  - `just obs traces --query '{ resource.service.name = "lsstack" && duration > 100ms }'`
  - `just obs metrics --query '<PromQL>'`
  - `just obs correlate <request-id>`
  - `just obs --json <command>` for machine-readable output
- Generate traffic before querying and allow for bounded exporter ingestion.
  `just obs check` verifies app logs, Nginx logs, metrics, and traces.
- For a latency task, define the workflow and threshold first, reproduce it,
  inspect matching spans and request-duration metrics, change one bottleneck,
  then repeat the same measurement. Preserve before/after evidence.

## Browser inspection

- The repository uses pinned `@playwright/cli` through `pnpm dlx`; do not use
  `npx`. Run `just browser-install` once when Chromium is absent.
- Keep every flow in a named artifact session:
  `just browser <label> open <application-url>`, then `snapshot`, interact using
  fresh element references, and re-snapshot after navigation or major DOM
  changes.
- Use `just browser <label> screenshot`, `requests`, `request <index>`,
  `console warning`, `tracing-start`, and `tracing-stop` for UI and request
  debugging. Artifacts stay under ignored `output/playwright/<label>/`.
- Check keyboard focus, labels, validation, narrow layouts, HTMX transitions,
  browser console output, and that no CDN assets are requested.

## Commits, pull requests, and cleanup

- Inspect `git status` and `git diff` before staging. Stage only intended files,
  inspect `git diff --cached`, and keep commits focused.
- Use Conventional Commit subjects such as `feat(scope): add behavior`,
  `fix(scope): correct failure`, `test(scope): cover edge case`, or
  `chore(tooling): improve workflow`. Use imperative, concise summaries.
- Before pushing, run `git diff --check`, `just check`, and the relevant
  integration/service tests. Report anything not run.
- Verify `gh auth status`, push the feature branch normally, and create a draft
  pull request with `gh pr create --draft --fill`. Include verification and any
  migration or operational notes. Inspect CI with `gh pr checks`.
- Never run `gh pr merge`; merging is a human task. Never force-push, hard
  reset, force-clean, forcibly delete branches/worktrees, or delete remote
  branches. The repo-local Codex hook blocks these common command forms, but it
  is a guardrail rather than a security boundary. Review or refresh its trust
  through `/hooks` when Codex reports that it changed.
- After a human merge, update the primary checkout, stop the feature stack, and
  run `just worktree-clean codex/<short-task>` from another checkout. The
  command verifies the GitHub PR is merged and the worktree is clean and
  stopped before removing the worktree and local branch. It never deletes the
  remote branch.
