# ADR 0004: Python, FastAPI, HTMX, SQLite, one container

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-21 |

## Context

The owner will not write code and has no stack preference. The requirements are: a scheduled job runner, a Slack Socket Mode client, a mobile web UI, a small relational store, and deployment as one container on a podman host behind Pangolin. The owner's other homelab services use Python, FastAPI, HTMX, and podman, so operational familiarity favors the same.

## Decision

- **Language:** Python 3.12 or later, managed with `uv` and a committed `uv.lock`.
- **Web:** FastAPI with Jinja templates and HTMX for in-place updates. No JavaScript build step.
- **Slack:** `slack_bolt` async app in Socket Mode, running in the same event loop as the web server.
- **Scheduler:** APScheduler with a database job store so jobs survive restarts; every job is idempotent against event state.
- **Storage:** SQLite in WAL mode through SQLAlchemy 2.x, with Alembic migrations. One file under `/data`.
- **Process:** one container, one process, one asyncio loop. A transactional outbox table decouples state changes from Slack delivery.
- **Quality:** `ruff`, `mypy --strict` on the domain and service layers, pytest, `pip-audit`, Dependabot, GitHub Actions.

## Reasoning

- **One process is enough.** Four users, a few dozen events a week. The outbox is the seam if the process ever needs to split.
- **SQLite over Postgres.** No second container, trivial backups with the online backup API, and write volume far below SQLite's limits. Postgres is a configuration change through SQLAlchemy if multi-household ever needs it.
- **HTMX over a JavaScript framework.** The web app is forms and lists on a phone. Server rendering keeps the security surface small and the build simple, and an agent can produce and review it quickly.
- **Bolt for Python over Bolt for JavaScript.** Both are first-party. Python keeps one language in the repository.
- **Strict typing on the core.** The ranking engine and state machine are where correctness matters and where tests and types pay off most.

## Consequences

- Contributors need Python and `uv`. The container build does not need anything else.
- The domain layer is pure Python with no I/O, which is what makes the ranking and lifecycle unit-testable without a database.
- SQLite means a single writer. The async stack must serialize writes, which SQLAlchemy and WAL mode handle at this scale. Long-running read transactions must not be held open across awaits.
- Single household in version 1. Every table carries `household_id` from the first migration so adding tenancy later is a routing change, not a schema rewrite.
