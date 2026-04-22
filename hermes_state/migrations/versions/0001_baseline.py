"""Baseline schema — initial Postgres tables for hermes_state.

Revision ID: 0001
Revises: None
Create Date: 2026-04-22
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute("""
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
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_sessions_source  ON sessions (source)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions (user_id, started_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_sessions_parent  ON sessions (parent_session_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions (started_at DESC)")

    op.execute("""
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
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, timestamp)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_messages_user    ON messages (user_id, timestamp DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_messages_tsv     ON messages USING GIN (content_tsv)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_messages_trgm    ON messages USING GIN (content gin_trgm_ops)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS memory_entries (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     TEXT NOT NULL DEFAULT 'default',
            kind        TEXT NOT NULL,
            content     TEXT NOT NULL,
            embedding   vector(1536),
            created_at  DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now()),
            updated_at  DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now())
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_memory_user_kind ON memory_entries (user_id, kind)")

    op.execute("""
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
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_cron_due ON cron_jobs (enabled, next_run_at) WHERE enabled")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS cron_jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS memory_entries CASCADE")
    op.execute("DROP TABLE IF EXISTS messages CASCADE")
    op.execute("DROP TABLE IF EXISTS sessions CASCADE")
