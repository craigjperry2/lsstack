set shell := ["bash", "-uc"]

# List the supported developer and agent commands.
default:
    @just --list

# Create .env when needed and install the exact locked Python dependency set.
setup:
    python -m tools.dev setup

# Start this worktree's isolated dependencies, migrate, app, and worker.
up:
    python -m tools.dev up

# Stop this worktree's host processes and containers while preserving volumes.
down:
    python -m tools.dev down

# Restart this worktree's complete application stack.
restart:
    python -m tools.dev restart

# Show owned host processes, Compose services, ports, and URLs.
status *args:
    python -m tools.dev status {{args}}

# Print application, Grafana, PostgreSQL, and OTLP endpoints.
urls *args:
    python -m tools.dev urls {{args}}

# Print status plus bounded host and container logs.
diagnose:
    python -m tools.dev diagnose

# Read host or container logs: just logs [app|worker|SERVICE|all] [--follow ...].
logs *args:
    python -m tools.dev logs {{args}}

# Upgrade the current worktree's application schema to Alembic head.
db-migrate:
    python -m tools.dev db-migrate

# Generate a candidate Alembic migration; always review the generated file.
db-revision message:
    python -m tools.dev db-revision {{quote(message)}}

# Idempotently create the documented local demo account and sample tasks.
db-seed:
    python -m tools.dev db-seed

# Reset only this worktree's volumes; run once without CONFIRM to see the token.
db-reset confirm="":
    python -m tools.dev db-reset {{quote(confirm)}}

# Run pytest with arbitrary arguments, using this worktree's allocated environment.
test *args:
    python -m tools.dev exec -- uv run pytest {{args}}

# Run architecture and unit tests without a database.
test-fast:
    uv run pytest tests/architecture tests/unit

# Run unit tests only.
test-unit:
    uv run pytest tests/unit

# Run architecture boundary tests only.
test-architecture:
    uv run pytest tests/architecture

# Start the stack and run all integration tests against disposable test data.
test-integration:
    python -m tools.dev up
    python -m tools.dev exec --set RUN_DATABASE_TESTS=1 -- uv run pytest tests/integration

# Start the stack and run in-process plus database-backed performance checks.
test-performance:
    python -m tools.dev up
    python -m tools.dev exec --set RUN_DATABASE_TESTS=1 -- uv run pytest tests/performance

# Start the stack and run the real Nginx/app/outbox/worker service checks.
test-service:
    python -m tools.dev up
    python -m tools.dev exec --set RUN_DATABASE_TESTS=1 --set RUN_SERVICE_TESTS=1 -- uv run pytest tests/integration/test_service_end_to_end.py

# Start the stack and run every test, including database and service checks.
test-all:
    python -m tools.dev up
    python -m tools.dev exec --set RUN_DATABASE_TESTS=1 --set RUN_SERVICE_TESTS=1 -- uv run pytest

# Run all non-mutating quality gates and the fast test suite.
check:
    uv run ruff format --check .
    uv run ruff check .
    uv run basedpyright
    uv run lint-imports
    uv run pytest tests/architecture tests/unit tests/tooling

# Apply formatting and safe Ruff fixes.
fix:
    uv run ruff format .
    uv run ruff check --fix .

# Run both configured prek stages across every tracked file.
prek:
    uv run prek run --all-files --hook-stage pre-commit
    uv run prek run --all-files --hook-stage pre-push

# Install the repository's pre-commit and pre-push hooks.
hooks-install:
    uv run prek install --hook-type pre-commit --hook-type pre-push

# Query Loki, Tempo, and Prometheus: just obs [--json] <logs|traces|trace|metrics|correlate|check>.
obs *args:
    python -m tools.dev obs {{args}}

# Install the pinned Playwright CLI's default Chromium browser with pnpm.
browser-install:
    python -m tools.dev browser-install

# Run a pinned Playwright CLI command in output/playwright/<session>.
browser session *args:
    python -m tools.dev browser {{quote(session)}} {{args}}

# Remove a clean, stopped worktree only after its GitHub PR is merged.
worktree-clean branch:
    python -m tools.dev worktree-clean {{quote(branch)}}
