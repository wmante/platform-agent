# Phase 1 — Postgres Backend: Agent Handoff

**Date:** 2026-04-22
**Branch:** `main` (4 commits ahead of origin)
**Scope:** `hermes_state/` package refactor + Postgres backend

---

## Plan references

- `docs/implementation_plan.md` — full 3-phase plan
- `docs/phase1_postgres_backend.md` — step-by-step Phase 1 guide

---

## What was completed and committed (4 commits)

### Commit 1 — Package skeleton + StateBackend Protocol + SQLiteBackend

Deleted monolithic `hermes_state.py`. Created `hermes_state/` package:

- `hermes_state/backend.py` — `StateBackend` `typing.Protocol` with all public method stubs
- `hermes_state/sqlite_backend.py` — original `SessionDB` code moved here, class renamed `SQLiteBackend(StateBackend)`
- `hermes_state/__init__.py` — factory `SessionDB = _make_session_db(...)`: returns `SQLiteBackend` by default, `PostgresBackend` when `HERMES_DB_URL` starts with `postgresql`
- `pyproject.toml` — `hermes_state` moved from `py-modules` to `packages.find.include`; new `[postgres]` optional-dep group: `sqlalchemy[asyncio]>=2.0`, `asyncpg>=0.29`, `pgvector>=0.3`, `alembic>=1.13`, `psycopg2-binary>=2.9`

### Commit 2 — Delete old hermes_state.py (housekeeping)

### Commit 3 — PostgresBackend + Alembic

- `hermes_state/postgres_backend.py` — full implementation:
  - Async SQLAlchemy + asyncpg engine; background thread runs a dedicated `asyncio` event loop; all public methods are synchronous wrappers via `asyncio.run_coroutine_threadsafe()`
  - All 26 session columns matching SQLiteBackend exactly
  - Full-text search: `tsvector`/`plainto_tsquery` for Latin text, `ILIKE`/`pg_trgm` fallback for CJK
  - 3-message context window in `search_messages` (parity with SQLite)
  - `memory_entries` and `cron_jobs` tables in schema SQL (Phase 2/3 ready)
- `hermes_state/migrations/env.py` — async Alembic env (reads `HERMES_DB_URL`)
- `hermes_state/migrations/script.py.mako` — standard template
- `hermes_state/migrations/versions/0001_baseline.py` — complete baseline migration with all tables and indexes
- `alembic.ini` — at repo root, `script_location = hermes_state/migrations`

---

## What is written but NOT yet committed (3 items)

### 1. `hermes_cli/main.py`

`cmd_db` handler and `hermes db` subparser added:

- `cmd_db(args)` and `_cmd_db_migrate_from_sqlite(args)` functions added around line 4354
- `hermes db` subparser + `migrate-from-sqlite` sub-subcommand added around line 7594
- Supports `--dry-run` flag (prints counts, writes nothing)
- Reads from `SQLiteBackend(HERMES_HOME/state.db)`, writes to `PostgresBackend(HERMES_DB_URL)`

### 2. `tests/test_hermes_state.py`

Fully refactored for dual-backend parametrization:

- `db` fixture parametrized on `["sqlite", "postgres"]`; Postgres variant skipped unless `HERMES_TEST_PG_URL` is set
- Postgres fixture cleans up all sessions after each test
- `TestPruneSessions` and `TestSchemaInit` tests that access `db._conn` directly call `pytest.skip()` when running against Postgres
- All `SessionDB.sanitize_title(...)` / `SessionDB._sanitize_fts5_query(...)` / `SessionDB._contains_cjk(...)` calls updated to `SQLiteBackend.xxx` (because `SessionDB` is now a factory function, not a class)
- Verified passing with SQLite: 10 passed, 10 skipped (postgres skipped) for `TestSessionLifecycle`

### 3. `docker/compose.dev.yaml`

Local dev Postgres stack: `pgvector/pgvector:pg16`, `POSTGRES_DB/USER/PASSWORD=hermes`, port 5432, healthcheck.

---

## Next immediate actions

### Step 1 — Commit the 3 uncommitted items

```bash
git add hermes_cli/main.py tests/test_hermes_state.py docker/compose.dev.yaml
git commit -m "feat: add hermes db CLI, parametrized tests, docker/compose.dev.yaml

Co-Authored-By: Oz <oz-agent@warp.dev>"
```

### Step 2 — Run the full test suite (SQLite path, no Postgres needed)

```bash
scripts/run_tests.sh
```

Check for other test files that call `SessionDB.somestaticmethod()` — `SessionDB` is now a callable factory, not a class:

```bash
grep -r "SessionDB\." tests/ --include="*.py" | grep -v "test_hermes_state.py"
```

### Step 3 — Validate with Postgres (requires Docker)

```bash
docker compose -f docker/compose.dev.yaml up -d

HERMES_TEST_PG_URL="postgresql+asyncpg://hermes:hermes@localhost:5432/hermes" \
  scripts/run_tests.sh

HERMES_DB_URL="postgresql+asyncpg://hermes:hermes@localhost:5432/hermes" \
  hermes db migrate-from-sqlite --dry-run
```

### Step 4 — Known issue to verify

The `UNIQUE (title)` constraint on `sessions.title` in Postgres vs SQLite's partial unique index (`WHERE title IS NOT NULL`). Postgres `UNIQUE` on a nullable column correctly allows multiple `NULL` values (SQL standard), so this should be fine — verify with:
- `test_multiple_empty_titles_no_conflict`
- `test_null_titles_not_unique`

### Step 5 — Move to Phase 2

Once Phase 1 tests are green, proceed to `docs/phase2_http_api.md` (FastAPI REST API + SSE streaming).

---

## Key design decisions (for context)

- `SessionDB` public name is preserved — all existing callers need zero changes
- `HERMES_DB_URL` unset → SQLite (backward compatible, zero migration)
- `PostgresBackend` uses a sync public API over an async engine to avoid forcing async on the entire codebase
- Static helpers (`sanitize_title`, `_sanitize_fts5_query`, `_contains_cjk`) live on `SQLiteBackend`; `PostgresBackend` duplicates `sanitize_title` independently
- Alembic is Postgres-only; SQLite schema continues to self-migrate via `_init_schema()`
