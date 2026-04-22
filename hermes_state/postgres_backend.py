"""
Postgres backend for Hermes Agent state storage.

Uses SQLAlchemy asyncio + asyncpg under the hood.  A background thread runs a
dedicated asyncio event loop for the lifetime of the backend.  All public
methods are synchronous (matching the SQLiteBackend / StateBackend interface)
and bridge into that event loop via asyncio.run_coroutine_threadsafe().

Requires the [postgres] optional-dependency group:
    pip install hermes-agent[postgres]

Activated automatically when HERMES_DB_URL starts with "postgresql".
"""

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

from hermes_state.backend import StateBackend

logger = logging.getLogger(__name__)

# ── Schema SQL ───────────────────────────────────────────────────────────────

SCHEMA_SQL = """
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
CREATE INDEX IF NOT EXISTS idx_sessions_source  ON sessions (source);
CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions (user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_parent  ON sessions (parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions (started_at DESC);

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
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_user    ON messages (user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_tsv     ON messages USING GIN (content_tsv);
CREATE INDEX IF NOT EXISTS idx_messages_trgm    ON messages USING GIN (content gin_trgm_ops);

CREATE TABLE IF NOT EXISTS memory_entries (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL DEFAULT 'default',
    kind        TEXT NOT NULL,
    content     TEXT NOT NULL,
    embedding   vector(1536),
    created_at  DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now()),
    updated_at  DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now())
);
CREATE INDEX IF NOT EXISTS idx_memory_user_kind ON memory_entries (user_id, kind);

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
"""

# ── Backend ──────────────────────────────────────────────────────────────────


class PostgresBackend(StateBackend):
    """
    Postgres-backed session storage via SQLAlchemy asyncio + asyncpg.

    A dedicated background thread runs a private asyncio event loop.
    All public methods are synchronous wrappers that delegate to async
    coroutines via run_coroutine_threadsafe(), so the caller thread
    never has to be async-aware.
    """

    MAX_TITLE_LENGTH = 100

    def __init__(self, db_url: str = None, **kwargs):
        try:
            from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
            from sqlalchemy.orm import sessionmaker
            from sqlalchemy import text
        except ImportError as exc:
            raise ImportError(
                "PostgresBackend requires the [postgres] extra: "
                "pip install hermes-agent[postgres]"
            ) from exc

        import os
        url = db_url or os.getenv("HERMES_DB_URL", "")
        if not url:
            raise ValueError("HERMES_DB_URL must be set to use PostgresBackend")

        self._text = text

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="hermes-pg-loop"
        )
        self._thread.start()

        self._engine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        self._session_factory = sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

        self._run(self._init_schema())

    # ── Async / sync bridge ───────────────────────────────────────────────────

    def _run(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    async def _init_schema(self):
        async with self._engine.begin() as conn:
            for stmt in self._split_schema_sql(SCHEMA_SQL):
                try:
                    await conn.execute(self._text(stmt))
                except Exception as exc:
                    # Index/extension creation may fail on restricted Postgres
                    # instances (e.g. no superuser). Log and continue; required
                    # tables come first and will fail loudly if they can't be created.
                    logger.debug("Schema statement skipped (%s): %.80s", exc, stmt.strip())

    @staticmethod
    def _split_schema_sql(sql: str) -> List[str]:
        """Split a multi-statement SQL string into individual statements."""
        stmts = []
        for stmt in sql.split(";"):
            stripped = stmt.strip()
            if stripped:
                stmts.append(stripped)
        return stmts

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        """Validate and sanitize a session title (mirrors SQLiteBackend)."""
        if not title:
            return None
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', title)
        cleaned = re.sub(
            r'[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]',
            '', cleaned,
        )
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        if not cleaned:
            return None
        if len(cleaned) > PostgresBackend.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Title too long ({len(cleaned)} chars, max {PostgresBackend.MAX_TITLE_LENGTH})"
            )
        return cleaned

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def create_session(
        self,
        session_id: str,
        source: str,
        model: str = None,
        model_config: Dict[str, Any] = None,
        system_prompt: str = None,
        user_id: str = None,
        parent_session_id: str = None,
    ) -> str:
        return self._run(self._create_session_async(
            session_id, source, model, model_config,
            system_prompt, user_id, parent_session_id,
        ))

    async def _create_session_async(
        self, session_id, source, model, model_config,
        system_prompt, user_id, parent_session_id,
    ) -> str:
        async with self._engine.begin() as conn:
            await conn.execute(self._text("""
                INSERT INTO sessions
                    (id, source, user_id, model, model_config,
                     system_prompt, parent_session_id, started_at)
                VALUES
                    (:id, :source, :user_id, :model, :model_config,
                     :system_prompt, :parent_session_id, :started_at)
                ON CONFLICT (id) DO NOTHING
            """), {
                "id": session_id,
                "source": source,
                "user_id": user_id or "default",
                "model": model,
                "model_config": json.dumps(model_config) if model_config else None,
                "system_prompt": system_prompt,
                "parent_session_id": parent_session_id,
                "started_at": time.time(),
            })
        return session_id

    def end_session(self, session_id: str, end_reason: str) -> None:
        self._run(self._end_session_async(session_id, end_reason))

    async def _end_session_async(self, session_id, end_reason):
        async with self._engine.begin() as conn:
            await conn.execute(self._text("""
                UPDATE sessions
                SET ended_at = :now, end_reason = :reason
                WHERE id = :id AND ended_at IS NULL
            """), {"now": time.time(), "reason": end_reason, "id": session_id})

    def reopen_session(self, session_id: str) -> None:
        self._run(self._reopen_session_async(session_id))

    async def _reopen_session_async(self, session_id):
        async with self._engine.begin() as conn:
            await conn.execute(self._text("""
                UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = :id
            """), {"id": session_id})

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        self._run(self._update_system_prompt_async(session_id, system_prompt))

    async def _update_system_prompt_async(self, session_id, system_prompt):
        async with self._engine.begin() as conn:
            await conn.execute(self._text("""
                UPDATE sessions SET system_prompt = :sp WHERE id = :id
            """), {"sp": system_prompt, "id": session_id})

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None,
        cost_status: Optional[str] = None,
        cost_source: Optional[str] = None,
        pricing_version: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None,
        absolute: bool = False,
    ) -> None:
        self._run(self._update_token_counts_async(
            session_id, input_tokens, output_tokens, model,
            cache_read_tokens, cache_write_tokens, reasoning_tokens,
            estimated_cost_usd, actual_cost_usd, cost_status,
            cost_source, pricing_version, billing_provider,
            billing_base_url, billing_mode, absolute,
        ))

    async def _update_token_counts_async(
        self, session_id, input_tokens, output_tokens, model,
        cache_read_tokens, cache_write_tokens, reasoning_tokens,
        estimated_cost_usd, actual_cost_usd, cost_status,
        cost_source, pricing_version, billing_provider,
        billing_base_url, billing_mode, absolute,
    ):
        params = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "reasoning_tokens": reasoning_tokens,
            "estimated_cost_usd": estimated_cost_usd,
            "actual_cost_usd": actual_cost_usd,
            "cost_status": cost_status,
            "cost_source": cost_source,
            "pricing_version": pricing_version,
            "billing_provider": billing_provider,
            "billing_base_url": billing_base_url,
            "billing_mode": billing_mode,
            "model": model,
            "id": session_id,
        }
        if absolute:
            sql = """
                UPDATE sessions SET
                    input_tokens        = :input_tokens,
                    output_tokens       = :output_tokens,
                    cache_read_tokens   = :cache_read_tokens,
                    cache_write_tokens  = :cache_write_tokens,
                    reasoning_tokens    = :reasoning_tokens,
                    estimated_cost_usd  = COALESCE(:estimated_cost_usd, 0),
                    actual_cost_usd     = CASE
                        WHEN :actual_cost_usd IS NULL THEN actual_cost_usd
                        ELSE :actual_cost_usd
                    END,
                    cost_status         = COALESCE(:cost_status, cost_status),
                    cost_source         = COALESCE(:cost_source, cost_source),
                    pricing_version     = COALESCE(:pricing_version, pricing_version),
                    billing_provider    = COALESCE(billing_provider, :billing_provider),
                    billing_base_url    = COALESCE(billing_base_url, :billing_base_url),
                    billing_mode        = COALESCE(billing_mode, :billing_mode),
                    model               = COALESCE(model, :model)
                WHERE id = :id
            """
        else:
            sql = """
                UPDATE sessions SET
                    input_tokens        = input_tokens + :input_tokens,
                    output_tokens       = output_tokens + :output_tokens,
                    cache_read_tokens   = cache_read_tokens + :cache_read_tokens,
                    cache_write_tokens  = cache_write_tokens + :cache_write_tokens,
                    reasoning_tokens    = reasoning_tokens + :reasoning_tokens,
                    estimated_cost_usd  = COALESCE(estimated_cost_usd, 0) + COALESCE(:estimated_cost_usd, 0),
                    actual_cost_usd     = CASE
                        WHEN :actual_cost_usd IS NULL THEN actual_cost_usd
                        ELSE COALESCE(actual_cost_usd, 0) + :actual_cost_usd
                    END,
                    cost_status         = COALESCE(:cost_status, cost_status),
                    cost_source         = COALESCE(:cost_source, cost_source),
                    pricing_version     = COALESCE(:pricing_version, pricing_version),
                    billing_provider    = COALESCE(billing_provider, :billing_provider),
                    billing_base_url    = COALESCE(billing_base_url, :billing_base_url),
                    billing_mode        = COALESCE(billing_mode, :billing_mode),
                    model               = COALESCE(model, :model)
                WHERE id = :id
            """
        async with self._engine.begin() as conn:
            await conn.execute(self._text(sql), params)

    def ensure_session(
        self,
        session_id: str,
        source: str = "unknown",
        model: str = None,
    ) -> None:
        self._run(self._ensure_session_async(session_id, source, model))

    async def _ensure_session_async(self, session_id, source, model):
        async with self._engine.begin() as conn:
            await conn.execute(self._text("""
                INSERT INTO sessions (id, source, model, started_at)
                VALUES (:id, :source, :model, :started_at)
                ON CONFLICT (id) DO NOTHING
            """), {
                "id": session_id, "source": source,
                "model": model, "started_at": time.time(),
            })

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._run(self._get_session_async(session_id))

    async def _get_session_async(self, session_id) -> Optional[Dict[str, Any]]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                self._text("SELECT * FROM sessions WHERE id = :id"),
                {"id": session_id},
            )
            row = result.mappings().first()
        return dict(row) if row else None

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        return self._run(self._resolve_session_id_async(session_id_or_prefix))

    async def _resolve_session_id_async(self, prefix) -> Optional[str]:
        exact = await self._get_session_async(prefix)
        if exact:
            return exact["id"]
        escaped = prefix.replace("%", "\\%").replace("_", "\\_")
        async with self._engine.connect() as conn:
            result = await conn.execute(self._text("""
                SELECT id FROM sessions
                WHERE id LIKE :prefix ESCAPE '\\'
                ORDER BY started_at DESC LIMIT 2
            """), {"prefix": f"{escaped}%"})
            matches = [row[0] for row in result.fetchall()]
        return matches[0] if len(matches) == 1 else None

    def set_session_title(self, session_id: str, title: str) -> bool:
        return self._run(self._set_session_title_async(session_id, title))

    async def _set_session_title_async(self, session_id, title) -> bool:
        title = self.sanitize_title(title)
        async with self._engine.begin() as conn:
            if title:
                result = await conn.execute(self._text("""
                    SELECT id FROM sessions WHERE title = :title AND id != :id
                """), {"title": title, "id": session_id})
                conflict = result.first()
                if conflict:
                    raise ValueError(
                        f"Title '{title}' is already in use by session {conflict[0]}"
                    )
            result = await conn.execute(self._text("""
                UPDATE sessions SET title = :title WHERE id = :id
            """), {"title": title, "id": session_id})
            return result.rowcount > 0

    def get_session_title(self, session_id: str) -> Optional[str]:
        return self._run(self._get_session_title_async(session_id))

    async def _get_session_title_async(self, session_id) -> Optional[str]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                self._text("SELECT title FROM sessions WHERE id = :id"),
                {"id": session_id},
            )
            row = result.first()
        return row[0] if row else None

    def get_session_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        return self._run(self._get_session_by_title_async(title))

    async def _get_session_by_title_async(self, title) -> Optional[Dict[str, Any]]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                self._text("SELECT * FROM sessions WHERE title = :title"),
                {"title": title},
            )
            row = result.mappings().first()
        return dict(row) if row else None

    def resolve_session_by_title(self, title: str) -> Optional[str]:
        return self._run(self._resolve_session_by_title_async(title))

    async def _resolve_session_by_title_async(self, title) -> Optional[str]:
        exact = await self._get_session_by_title_async(title)
        escaped = title.replace("%", "\\%").replace("_", "\\_")
        async with self._engine.connect() as conn:
            result = await conn.execute(self._text("""
                SELECT id FROM sessions
                WHERE title LIKE :pattern ESCAPE '\\'
                ORDER BY started_at DESC
            """), {"pattern": f"{escaped} #%"})
            numbered = result.fetchall()
        if numbered:
            return numbered[0][0]
        if exact:
            return exact["id"]
        return None

    def get_next_title_in_lineage(self, base_title: str) -> str:
        return self._run(self._get_next_title_in_lineage_async(base_title))

    async def _get_next_title_in_lineage_async(self, base_title) -> str:
        m = re.match(r'^(.*?) #(\d+)$', base_title)
        base = m.group(1) if m else base_title
        escaped = base.replace("%", "\\%").replace("_", "\\_")
        async with self._engine.connect() as conn:
            result = await conn.execute(self._text("""
                SELECT title FROM sessions
                WHERE title = :base OR title LIKE :pattern ESCAPE '\\'
            """), {"base": base, "pattern": f"{escaped} #%"})
            existing = [row[0] for row in result.fetchall()]
        if not existing:
            return base
        max_num = 1
        for t in existing:
            nm = re.match(r'^.* #(\d+)$', t)
            if nm:
                max_num = max(max_num, int(nm.group(1)))
        return f"{base} #{max_num + 1}"

    def get_compression_tip(self, session_id: str) -> Optional[str]:
        return self._run(self._get_compression_tip_async(session_id))

    async def _get_compression_tip_async(self, session_id) -> str:
        current = session_id
        for _ in range(100):
            async with self._engine.connect() as conn:
                result = await conn.execute(self._text("""
                    SELECT id FROM sessions
                    WHERE parent_session_id = :cur
                      AND started_at >= (
                          SELECT ended_at FROM sessions
                          WHERE id = :cur AND end_reason = 'compression'
                      )
                    ORDER BY started_at DESC LIMIT 1
                """), {"cur": current})
                row = result.first()
            if row is None:
                return current
            current = row[0]
        return current

    def list_sessions_rich(
        self,
        source: str = None,
        exclude_sources: List[str] = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
        project_compression_tips: bool = True,
    ) -> List[Dict[str, Any]]:
        return self._run(self._list_sessions_rich_async(
            source, exclude_sources, limit, offset,
            include_children, project_compression_tips,
        ))

    async def _list_sessions_rich_async(
        self, source, exclude_sources, limit, offset,
        include_children, project_compression_tips,
    ) -> List[Dict[str, Any]]:
        wheres = []
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if not include_children:
            wheres.append("s.parent_session_id IS NULL")
        if source:
            wheres.append("s.source = :source")
            params["source"] = source
        if exclude_sources:
            placeholders = ", ".join(f":exc{i}" for i in range(len(exclude_sources)))
            wheres.append(f"s.source NOT IN ({placeholders})")
            for i, s in enumerate(exclude_sources):
                params[f"exc{i}"] = s
        where_sql = f"WHERE {' AND '.join(wheres)}" if wheres else ""
        query = f"""
            SELECT s.*,
                COALESCE(
                    (SELECT SUBSTRING(
                         REGEXP_REPLACE(COALESCE(m.content,''), E'[\\n\\r]+', ' ', 'g'),
                         1, 63)
                     FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user'
                       AND m.content IS NOT NULL
                     ORDER BY m.timestamp, m.id LIMIT 1),
                    ''
                ) AS _preview_raw,
                COALESCE(
                    (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                    s.started_at
                ) AS last_active
            FROM sessions s
            {where_sql}
            ORDER BY s.started_at DESC
            LIMIT :limit OFFSET :offset
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(self._text(query), params)
            rows = result.mappings().fetchall()

        sessions = []
        for row in rows:
            s = dict(row)
            raw = (s.pop("_preview_raw", "") or "").strip()
            s["preview"] = (raw[:60] + ("..." if len(raw) > 60 else "")) if raw else ""
            sessions.append(s)

        if project_compression_tips and not include_children:
            projected = []
            for s in sessions:
                if s.get("end_reason") != "compression":
                    projected.append(s)
                    continue
                tip_id = await self._get_compression_tip_async(s["id"])
                if tip_id == s["id"]:
                    projected.append(s)
                    continue
                tip_row = await self._get_session_rich_row_async(tip_id)
                if not tip_row:
                    projected.append(s)
                    continue
                merged = dict(s)
                for key in (
                    "id", "ended_at", "end_reason", "message_count",
                    "tool_call_count", "title", "last_active", "preview",
                    "model", "system_prompt",
                ):
                    if key in tip_row:
                        merged[key] = tip_row[key]
                merged["_lineage_root_id"] = s["id"]
                projected.append(merged)
            sessions = projected

        return sessions

    async def _get_session_rich_row_async(self, session_id) -> Optional[Dict[str, Any]]:
        query = """
            SELECT s.*,
                COALESCE(
                    (SELECT SUBSTRING(
                         REGEXP_REPLACE(COALESCE(m.content,''), E'[\\n\\r]+', ' ', 'g'),
                         1, 63)
                     FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user'
                       AND m.content IS NOT NULL
                     ORDER BY m.timestamp, m.id LIMIT 1),
                    ''
                ) AS _preview_raw,
                COALESCE(
                    (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                    s.started_at
                ) AS last_active
            FROM sessions s WHERE s.id = :id
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(self._text(query), {"id": session_id})
            row = result.mappings().first()
        if not row:
            return None
        s = dict(row)
        raw = (s.pop("_preview_raw", "") or "").strip()
        s["preview"] = (raw[:60] + ("..." if len(raw) > 60 else "")) if raw else ""
        return s

    # ── Message storage ───────────────────────────────────────────────────────

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str = None,
        tool_name: str = None,
        tool_calls: Any = None,
        tool_call_id: str = None,
        token_count: int = None,
        finish_reason: str = None,
        reasoning: str = None,
        reasoning_content: str = None,
        reasoning_details: Any = None,
        codex_reasoning_items: Any = None,
    ) -> int:
        return self._run(self._append_message_async(
            session_id, role, content, tool_name, tool_calls, tool_call_id,
            token_count, finish_reason, reasoning, reasoning_content,
            reasoning_details, codex_reasoning_items,
        ))

    async def _append_message_async(
        self, session_id, role, content, tool_name, tool_calls, tool_call_id,
        token_count, finish_reason, reasoning, reasoning_content,
        reasoning_details, codex_reasoning_items,
    ) -> int:
        num_tool_calls = 0
        if tool_calls is not None:
            num_tool_calls = len(tool_calls) if isinstance(tool_calls, list) else 1

        async with self._engine.begin() as conn:
            result = await conn.execute(self._text("""
                INSERT INTO messages
                    (session_id, role, content, tool_call_id, tool_calls,
                     tool_name, timestamp, token_count, finish_reason,
                     reasoning, reasoning_content, reasoning_details,
                     codex_reasoning_items)
                VALUES
                    (:session_id, :role, :content, :tool_call_id, :tool_calls,
                     :tool_name, :timestamp, :token_count, :finish_reason,
                     :reasoning, :reasoning_content, :reasoning_details,
                     :codex_reasoning_items)
                RETURNING id
            """), {
                "session_id": session_id,
                "role": role,
                "content": content,
                "tool_call_id": tool_call_id,
                "tool_calls": json.dumps(tool_calls) if tool_calls is not None else None,
                "tool_name": tool_name,
                "timestamp": time.time(),
                "token_count": token_count,
                "finish_reason": finish_reason,
                "reasoning": reasoning,
                "reasoning_content": reasoning_content,
                "reasoning_details": json.dumps(reasoning_details) if reasoning_details is not None else None,
                "codex_reasoning_items": json.dumps(codex_reasoning_items) if codex_reasoning_items is not None else None,
            })
            msg_id = result.scalar()

            if num_tool_calls > 0:
                await conn.execute(self._text("""
                    UPDATE sessions
                    SET message_count   = message_count + 1,
                        tool_call_count = tool_call_count + :n
                    WHERE id = :id
                """), {"n": num_tool_calls, "id": session_id})
            else:
                await conn.execute(self._text("""
                    UPDATE sessions SET message_count = message_count + 1 WHERE id = :id
                """), {"id": session_id})

        return msg_id

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        return self._run(self._get_messages_async(session_id))

    async def _get_messages_async(self, session_id) -> List[Dict[str, Any]]:
        async with self._engine.connect() as conn:
            result = await conn.execute(self._text("""
                SELECT * FROM messages WHERE session_id = :id ORDER BY timestamp, id
            """), {"id": session_id})
            rows = result.mappings().fetchall()
        out = []
        for row in rows:
            msg = dict(row)
            for field in ("tool_calls", "reasoning_details", "codex_reasoning_items"):
                if isinstance(msg.get(field), str):
                    try:
                        msg[field] = json.loads(msg[field])
                    except (json.JSONDecodeError, TypeError):
                        msg[field] = None
            out.append(msg)
        return out

    def get_messages_as_conversation(self, session_id: str) -> List[Dict[str, Any]]:
        return self._run(self._get_messages_as_conversation_async(session_id))

    async def _get_messages_as_conversation_async(self, session_id) -> List[Dict[str, Any]]:
        async with self._engine.connect() as conn:
            result = await conn.execute(self._text("""
                SELECT role, content, tool_call_id, tool_calls, tool_name,
                       reasoning, reasoning_content, reasoning_details, codex_reasoning_items
                FROM messages WHERE session_id = :id ORDER BY timestamp, id
            """), {"id": session_id})
            rows = result.mappings().fetchall()
        messages = []
        for row in rows:
            msg: Dict[str, Any] = {"role": row["role"], "content": row["content"]}
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_name"]:
                msg["tool_name"] = row["tool_name"]
            if row["tool_calls"]:
                raw = row["tool_calls"]
                try:
                    msg["tool_calls"] = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, TypeError):
                    msg["tool_calls"] = []
            if row["role"] == "assistant":
                if row["reasoning"]:
                    msg["reasoning"] = row["reasoning"]
                if row["reasoning_content"] is not None:
                    msg["reasoning_content"] = row["reasoning_content"]
                if row["reasoning_details"]:
                    raw = row["reasoning_details"]
                    try:
                        msg["reasoning_details"] = json.loads(raw) if isinstance(raw, str) else raw
                    except (json.JSONDecodeError, TypeError):
                        msg["reasoning_details"] = None
                if row["codex_reasoning_items"]:
                    raw = row["codex_reasoning_items"]
                    try:
                        msg["codex_reasoning_items"] = json.loads(raw) if isinstance(raw, str) else raw
                    except (json.JSONDecodeError, TypeError):
                        msg["codex_reasoning_items"] = None
            messages.append(msg)
        return messages

    # ── Search ────────────────────────────────────────────────────────────────

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        for ch in text:
            cp = ord(ch)
            if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
                    0x20000 <= cp <= 0x2A6DF or 0x3000 <= cp <= 0x303F or
                    0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF or
                    0xAC00 <= cp <= 0xD7AF):
                return True
        return False

    def search_messages(
        self,
        query: str,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return self._run(self._search_messages_async(
            query, source_filter, exclude_sources, role_filter, limit, offset
        ))

    async def _search_messages_async(
        self, query, source_filter, exclude_sources, role_filter, limit, offset
    ) -> List[Dict[str, Any]]:
        if not query or not query.strip():
            return []

        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        wheres = []
        idx = [0]

        def _add_param(val):
            key = f"p{idx[0]}"
            idx[0] += 1
            params[key] = val
            return f":{key}"

        is_cjk = self._contains_cjk(query)

        if is_cjk:
            # ILIKE fallback for CJK (pg_trgm provides the GIN index)
            pattern_key = _add_param(f"%{query}%")
            wheres.append(f"m.content ILIKE {pattern_key}")
            snippet_expr = f"SUBSTRING(m.content FROM GREATEST(1, POSITION({_add_param(query)} IN m.content) - 40) FOR 120)"
            params[f"p{idx[0]-1}"] = query  # positional search term
        else:
            # tsvector FTS
            tsq_key = _add_param(query)
            wheres.append(f"m.content_tsv @@ plainto_tsquery('simple', {tsq_key})")
            snippet_expr = f"ts_headline('simple', COALESCE(m.content,''), plainto_tsquery('simple', {tsq_key}), 'MaxWords=40,MinWords=10,StartSel=>>>,StopSel=<<<')"
            params[f"p{idx[0]-1}"] = query

        if source_filter is not None:
            phs = ", ".join(_add_param(s) for s in source_filter)
            wheres.append(f"s.source IN ({phs})")
        if exclude_sources is not None:
            phs = ", ".join(_add_param(s) for s in exclude_sources)
            wheres.append(f"s.source NOT IN ({phs})")
        if role_filter:
            phs = ", ".join(_add_param(r) for r in role_filter)
            wheres.append(f"m.role IN ({phs})")

        where_sql = " AND ".join(wheres)
        order_sql = "m.timestamp DESC" if is_cjk else "ts_rank(m.content_tsv, plainto_tsquery('simple', :q_rank)) DESC"
        if not is_cjk:
            params["q_rank"] = query

        sql = f"""
            SELECT
                m.id,
                m.session_id,
                m.role,
                {snippet_expr} AS snippet,
                m.content,
                m.timestamp,
                m.tool_name,
                s.source,
                s.model,
                s.started_at AS session_started
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT :limit OFFSET :offset
        """

        async with self._engine.connect() as conn:
            try:
                result = await conn.execute(self._text(sql), params)
                matches = [dict(r) for r in result.mappings().fetchall()]
            except Exception as exc:
                logger.debug("search_messages query failed: %s", exc)
                return []

        # Add surrounding context (1 before + 1 after the match)
        for match in matches:
            try:
                async with self._engine.connect() as conn:
                    ctx = await conn.execute(self._text("""
                        WITH target AS (
                            SELECT session_id, timestamp, id FROM messages WHERE id = :mid
                        )
                        SELECT role, content FROM (
                            SELECT m.id, m.timestamp, m.role, m.content
                            FROM messages m
                            JOIN target t ON t.session_id = m.session_id
                            WHERE (m.timestamp < t.timestamp)
                               OR (m.timestamp = t.timestamp AND m.id < t.id)
                            ORDER BY m.timestamp DESC, m.id DESC
                            LIMIT 1
                        ) prev
                        UNION ALL
                        SELECT role, content FROM messages WHERE id = :mid
                        UNION ALL
                        SELECT role, content FROM (
                            SELECT m.id, m.timestamp, m.role, m.content
                            FROM messages m
                            JOIN target t ON t.session_id = m.session_id
                            WHERE (m.timestamp > t.timestamp)
                               OR (m.timestamp = t.timestamp AND m.id > t.id)
                            ORDER BY m.timestamp ASC, m.id ASC
                            LIMIT 1
                        ) nxt
                    """), {"mid": match["id"]})
                    match["context"] = [
                        {"role": r[0], "content": (r[1] or "")[:200]}
                        for r in ctx.fetchall()
                    ]
            except Exception:
                match["context"] = []

        for match in matches:
            match.pop("content", None)
        return matches

    def search_sessions(
        self,
        source: str = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return self._run(self._search_sessions_async(source, limit, offset))

    async def _search_sessions_async(self, source, limit, offset) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        where = "WHERE source = :source" if source else ""
        if source:
            params["source"] = source
        async with self._engine.connect() as conn:
            result = await conn.execute(self._text(f"""
                SELECT * FROM sessions {where}
                ORDER BY started_at DESC LIMIT :limit OFFSET :offset
            """), params)
            return [dict(r) for r in result.mappings().fetchall()]

    # ── Utility ───────────────────────────────────────────────────────────────

    def session_count(self, source: str = None) -> int:
        return self._run(self._session_count_async(source))

    async def _session_count_async(self, source) -> int:
        params = {}
        where = ""
        if source:
            where = "WHERE source = :source"
            params["source"] = source
        async with self._engine.connect() as conn:
            result = await conn.execute(
                self._text(f"SELECT COUNT(*) FROM sessions {where}"), params
            )
            return result.scalar()

    def message_count(self, session_id: str = None) -> int:
        return self._run(self._message_count_async(session_id))

    async def _message_count_async(self, session_id) -> int:
        params = {}
        where = ""
        if session_id:
            where = "WHERE session_id = :id"
            params["id"] = session_id
        async with self._engine.connect() as conn:
            result = await conn.execute(
                self._text(f"SELECT COUNT(*) FROM messages {where}"), params
            )
            return result.scalar()

    # ── Export & cleanup ──────────────────────────────────────────────────────

    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = self.get_session(session_id)
        if not session:
            return None
        return {**session, "messages": self.get_messages(session_id)}

    def export_all(self, source: str = None) -> List[Dict[str, Any]]:
        sessions = self.search_sessions(source=source, limit=100_000)
        return [{**s, "messages": self.get_messages(s["id"])} for s in sessions]

    def clear_messages(self, session_id: str) -> None:
        self._run(self._clear_messages_async(session_id))

    async def _clear_messages_async(self, session_id):
        async with self._engine.begin() as conn:
            await conn.execute(
                self._text("DELETE FROM messages WHERE session_id = :id"),
                {"id": session_id},
            )
            await conn.execute(self._text("""
                UPDATE sessions SET message_count = 0, tool_call_count = 0
                WHERE id = :id
            """), {"id": session_id})

    def delete_session(self, session_id: str) -> bool:
        return self._run(self._delete_session_async(session_id))

    async def _delete_session_async(self, session_id) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                self._text("SELECT COUNT(*) FROM sessions WHERE id = :id"),
                {"id": session_id},
            )
            if result.scalar() == 0:
                return False
            await conn.execute(self._text("""
                UPDATE sessions SET parent_session_id = NULL
                WHERE parent_session_id = :id
            """), {"id": session_id})
            await conn.execute(
                self._text("DELETE FROM messages WHERE session_id = :id"),
                {"id": session_id},
            )
            await conn.execute(
                self._text("DELETE FROM sessions WHERE id = :id"),
                {"id": session_id},
            )
        return True

    def prune_sessions(self, older_than_days: int = 90, source: str = None) -> int:
        return self._run(self._prune_sessions_async(older_than_days, source))

    async def _prune_sessions_async(self, older_than_days, source) -> int:
        cutoff = time.time() - (older_than_days * 86400)
        params: Dict[str, Any] = {"cutoff": cutoff}
        where = "started_at < :cutoff AND ended_at IS NOT NULL"
        if source:
            where += " AND source = :source"
            params["source"] = source
        async with self._engine.begin() as conn:
            result = await conn.execute(
                self._text(f"SELECT id FROM sessions WHERE {where}"), params
            )
            session_ids = [row[0] for row in result.fetchall()]
            if not session_ids:
                return 0
            phs = ", ".join(f":sid{i}" for i in range(len(session_ids)))
            sid_params = {f"sid{i}": sid for i, sid in enumerate(session_ids)}
            await conn.execute(self._text(f"""
                UPDATE sessions SET parent_session_id = NULL
                WHERE parent_session_id IN ({phs})
            """), sid_params)
            await conn.execute(
                self._text(f"DELETE FROM messages WHERE session_id IN ({phs})"),
                sid_params,
            )
            await conn.execute(
                self._text(f"DELETE FROM sessions WHERE id IN ({phs})"),
                sid_params,
            )
        return len(session_ids)

    def close(self) -> None:
        try:
            self._run(self._engine.dispose())
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
