"""
hermes_state — backend-agnostic session storage for Hermes Agent.

Backend selection via HERMES_DB_URL:
  - unset / sqlite:// → SQLiteBackend  (default, zero migration required)
  - postgresql+asyncpg://… → PostgresBackend

All existing callers use ``SessionDB`` which remains the public name.
"""

import os

from hermes_state.sqlite_backend import (
    SQLiteBackend,
    DEFAULT_DB_PATH,
    SCHEMA_VERSION,
)


def _make_session_db(*args, **kwargs):
    url = os.getenv("HERMES_DB_URL", "")
    if url.startswith("postgresql"):
        from hermes_state.postgres_backend import PostgresBackend
        return PostgresBackend(*args, **kwargs)
    return SQLiteBackend(*args, **kwargs)


# Keep ``SessionDB`` as the public name — all import sites remain unchanged.
SessionDB = _make_session_db

# Re-export helpers that tests import directly from hermes_state
_sanitize_fts5_query = SQLiteBackend._sanitize_fts5_query
_contains_cjk = SQLiteBackend._contains_cjk

__all__ = [
    "SessionDB",
    "SQLiteBackend",
    "DEFAULT_DB_PATH",
    "SCHEMA_VERSION",
    "_sanitize_fts5_query",
    "_contains_cjk",
]
