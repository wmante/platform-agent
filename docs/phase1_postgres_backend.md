# Phase 1 — Postgres Backend

## Overview

Refactor `hermes_state.py` into a backend-agnostic `hermes_state/` package.
SQLite remains the default (zero behavior change). Postgres is opt-in via
`HERMES_DB_URL`. Includes pgvector, Alembic migrations, a SQLite→Postgres
migration CLI command, and a local Docker Compose setup.

**Warp plan ID:** `fc75958b-f0b9-4e0a-b8b5-e818df8bcc36`

---

## Prerequisites

- Python virtual environment activated (`source venv/bin/activate`)
- Docker available locally for running `docker/compose.dev.yaml`
- Read `AGENTS.md` before writing any code — especially the sections on
  `get_hermes_home()`, profiles, and test isolation

---

## Context: Current State

| File | Role |
|------|------|
| `hermes_state.py` | 1 450-line monolithic `SessionDB` class, SQLite-only, `SCHEMA_VERSION = 7` |
| `hermes_constants.py` | `get_hermes_home()` — **always use this for paths** |
| `pyproject.toml` | `asyncpg>=0.29` already in `matrix` extra; `fastapi/uvicorn` in `web` extra |

### SessionDB public interface (all methods must be preserved)

```
create_session / end_session / reopen_session / ensure_session
get_session / resolve_session_id / get_session_by_title
resolve_session_by_title / get_next_title_in_lineage / get_compression_tip
set_session_title / get_session_title
update_system_prompt / update_token_counts
list_sessions_rich / search_sessions
append_message / get_messages / get_messages_as_conversation
search_messages
session_count / message_count
export_session / export_all
clear_messages / delete_session / prune_sessions
close
```

### sessions table columns (all 26 must exist in Postgres)

`id`, `source`, `user_id`, `model`, `model_config`, `system_prompt`,
`parent_session_id`, `started_at`, `ended_at`, `end_reason`,
`message_count`, `tool_call_count`, `input_tokens`, `output_tokens`,
`cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`,
`billing_provider`, `billing_base_url`, `billing_mode`,
`estimated_cost_usd`, `actual_cost_usd`, `cost_status`, `cost_source`,
`pricing_version`, `title`

---

## Step-by-step implementation

### Step 1 — Create `hermes_state/` package skeleton

Create the following files (contents described below):

```
hermes_state/
├── __init__.py
├── backend.py
├── sqlite_backend.py
├── postgres_backend.py
└── migrations/
    ├── env.py
    ├── script.py.mako
    └── versions/
        └── 0001_baseline.py
```

**Commit after this step.**

---

### Step 2 — `hermes_state/backend.py`

Define the `StateBackend` `Protocol` with every public method from current
`SessionDB`. Use `typing.Protocol` with `runtime_checkable=True`.

```python
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

@runtime_checkable
class StateBackend(Protocol):
    def create_session(self, session_id: str, source: str, ...) -> str: ...
    def end_session(self, session_id: str, end_reason: str) -> None: ...
    # ... one stub per public method
    def close(self) -> None: ...
```

Keep all parameter signatures identical to current `SessionDB` so callers
need zero changes.

---

### Step 3 — `hermes_state/sqlite_backend.py`

Move the entire content of `hermes_state.py` here. Rename the class
`SQLiteBackend`. Add `from hermes_state.backend import StateBackend` and
declare `class SQLiteBackend(StateBackend):`.

No logic changes — this is a pure move + rename.

**Commit after this step.**

---

### Step 4 — `hermes_state/__init__.py`

```python
import os
from hermes_state.sqlite_backend import SQLiteBackend

def _make_session_db(*args, **kwargs):
    url = os.getenv("HERMES_DB_URL", "")
    if url.startswith("postgresql"):
        from hermes_state.postgres_backend import PostgresBackend
        return PostgresBackend(*args, **kwargs)
    return SQLiteBackend(*args, **kwargs)

# Keep SessionDB as the public name so all import sites work unchanged
SessionDB = _make_session_db
```

Also re-export `DEFAULT_DB_PATH` and `SCHEMA_VERSION` for any importers that
use them directly.

**Commit after this step. Delete `hermes_state.py`.**

---

### Step 5 — Update `pyproject.toml`

1. Remove `hermes_state` from `py-modules` (it's now a package, not a module).
2. Add `hermes_state` to `packages.find.include`.
3. Add the new optional dep group:

```toml
[project.optional-dependencies]
postgres = [
  "sqlalchemy[asyncio]>=2.0,<3",
  "asyncpg>=0.29,<1",
  "pgvector>=0.3,<1",
  "alembic>=1.13,<2",
  "psycopg2-binary>=2.9,<3",
]
```

**Commit after this step.**

---

### Step 6 — `hermes_state/postgres_backend.py`

#### Postgres schema (full, production-ready)

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    user_id             TEXT NOT NULL DEFAULT 'default',
    model               TEXT,
    model_config        JSONB,
    system_prompt       TEXT,
    parent_session_id   TEXT REFERENCES sessions(id),
    started_at          DOUBLE PRECISION NOT NULL,
    ended_at            DOUBLE PRECISION,
    end_reason          TEXT,
    message_count       INTEGER NOT NULL DEFAULT 0,
    tool_call_count     INTEGER NOT NULL DEFAULT 0,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens    INTEGER NOT NULL DEFAULT 0,
    billing_provider    TEXT,
    billing_base_url    TEXT,
    billing_mode        TEXT,
    estimated_cost_usd  DOUBLE PRECISION,
    actual_cost_usd     DOUBLE PRECISION,
    cost_status         TEXT,
    cost_source         TEXT,
    pricing_version     TEXT,
    title               TEXT,
    UNIQUE (title)
);
CREATE INDEX IF NOT EXISTS idx_sessions_source     ON sessions (source);
CREATE INDEX IF NOT EXISTS idx_sessions_user       ON sessions (user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_parent     ON sessions (parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started    ON sessions (started_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id                      BIGSERIAL PRIMARY KEY,
    session_id              TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id                 TEXT NOT NULL DEFAULT 'default',
    role                    TEXT NOT NULL,
    content                 TEXT,
    tool_call_id            TEXT,
    tool_calls              JSONB,
    tool_name               TEXT,
    timestamp               DOUBLE PRECISION NOT NULL,
    token_count             INTEGER,
    finish_reason           TEXT,
    reasoning               TEXT,
    reasoning_content       TEXT,
    reasoning_details       JSONB,
    codex_reasoning_items   JSONB,
    embedding               vector(1536),
    content_tsv             tsvector GENERATED ALWAYS AS (
                                to_tsvector('simple', coalesce(content, ''))
                            ) STORED
);
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages (session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_user      ON messages (user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_tsv       ON messages USING GIN (content_tsv);
CREATE INDEX IF NOT EXISTS idx_messages_trgm      ON messages USING GIN (content gin_trgm_ops);
-- HNSW index created separately after table exists (pgvector requires data type)
-- CREATE INDEX CONCURRENTLY idx_messages_embedding ON messages USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS memory_entries (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL DEFAULT 'default',
    kind        TEXT NOT NULL,
    content     TEXT NOT NULL,
    embedding   vector(1536),
    created_at  DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now()),
    updated_at  DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now())
);
CREATE INDEX IF NOT EXISTS idx_memory_user_kind  ON memory_entries (user_id, kind);

CREATE TABLE IF NOT EXISTS cron_jobs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      TEXT NOT NULL DEFAULT 'default',
    schedule     TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    deliver      TEXT,
    enabled      BOOLEAN NOT NULL DEFAULT true,
    last_run_at  DOUBLE PRECISION,
    next_run_at  DOUBLE PRECISION,
    metadata     JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_cron_due ON cron_jobs (enabled, next_run_at) WHERE enabled;
```

#### Async/sync bridge

`PostgresBackend` uses `sqlalchemy.ext.asyncio.AsyncEngine`. A background
thread keeps a private `asyncio` event loop running for the lifetime of the
backend. All public methods are **synchronous** and delegate to coroutines
via `asyncio.run_coroutine_threadsafe(coro, self._loop).result()`.

```python
import asyncio
import threading
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

class PostgresBackend:
    def __init__(self, db_url: str, **kwargs):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._engine = create_async_engine(db_url, pool_pre_ping=True)
        self._session_factory = sessionmaker(self._engine, class_=AsyncSession, expire_on_commit=False)
        self._run(self._init_schema())

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _init_schema(self):
        async with self._engine.begin() as conn:
            await conn.execute(text(SCHEMA_SQL))

    def create_session(self, session_id, source, ...) -> str:
        return self._run(self._create_session_async(session_id, source, ...))

    # ... mirror every public method as a sync wrapper over an async coroutine
```

Implement all methods from the `StateBackend` Protocol. For search methods,
use `tsvector @@ to_tsquery` for FTS and `%` operator (pg_trgm) as fallback.
SSE vector search (`embedding <=>`) is wired but optional (only runs when
`embedding` column is non-null).

**Commit after this step.**

---

### Step 7 — Alembic setup

```
hermes_state/migrations/env.py          # standard async Alembic env
hermes_state/migrations/script.py.mako  # standard template
hermes_state/migrations/versions/0001_baseline.py  # contains the full SCHEMA_SQL above
```

`alembic.ini` at repo root pointing at `hermes_state/migrations/`.

The baseline migration is the only migration needed; future schema changes
add new versioned files under `versions/`.

**Commit after this step.**

---

### Step 8 — `hermes db migrate-from-sqlite` CLI command

In `hermes_cli/main.py`, add a `db` subcommand group. The
`migrate-from-sqlite` sub-subcommand:

1. Opens `SQLiteBackend` from `HERMES_HOME/state.db`
2. Opens `PostgresBackend` from `HERMES_DB_URL`
3. Iterates `export_all()` from SQLite
4. For each session: calls `create_session()` then `append_message()` for
   each message on the Postgres backend
5. Prints a progress line per session and a final summary (sessions migrated,
   messages migrated, errors)

Flag `--dry-run` prints counts without writing.

**Commit after this step.**

---

### Step 9 — Docker Compose for local development

Create `docker/compose.dev.yaml`:

```yaml
version: "3.9"
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: hermes
      POSTGRES_USER: hermes
      POSTGRES_PASSWORD: hermes
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U hermes"]
      interval: 5s
      timeout: 3s
      retries: 10

volumes:
  pgdata:
```

Usage:
```bash
docker compose -f docker/compose.dev.yaml up -d
export HERMES_DB_URL="postgresql+asyncpg://hermes:hermes@localhost:5432/hermes"
```

**Commit after this step.**

---

### Step 10 — Tests

Locate existing `SessionDB` tests (likely in `tests/test_hermes_state.py` or
`tests/`). Parametrize them to run against both backends:

```python
import pytest, os
from hermes_state.sqlite_backend import SQLiteBackend
from hermes_state.postgres_backend import PostgresBackend

PG_URL = os.getenv("HERMES_TEST_PG_URL")

@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG_URL, reason="HERMES_TEST_PG_URL not set"))])
def db(tmp_path, request):
    if request.param == "sqlite":
        yield SQLiteBackend(db_path=tmp_path / "test.db")
    else:
        yield PostgresBackend(db_url=PG_URL)
```

All existing test functions should accept `db` as their only fixture.

**Commit after this step.**

---

### Step 11 — Final validation

```bash
# SQLite path (no env var) — must pass
scripts/run_tests.sh

# Postgres path — requires docker compose to be running
HERMES_TEST_PG_URL="postgresql+asyncpg://hermes:hermes@localhost:5432/hermes" \
  scripts/run_tests.sh

# Migration smoke test
HERMES_DB_URL="postgresql+asyncpg://hermes:hermes@localhost:5432/hermes" \
  hermes db migrate-from-sqlite --dry-run
```

---

## Commit checklist

1. Package skeleton + `backend.py`
2. `sqlite_backend.py` (pure move)
3. `__init__.py` factory + delete `hermes_state.py`
4. `pyproject.toml` updates
5. `postgres_backend.py` (full implementation)
6. Alembic setup
7. `hermes db migrate-from-sqlite` CLI
8. `docker/compose.dev.yaml`
9. Parametrized tests
