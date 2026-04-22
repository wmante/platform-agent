"""
FastAPI dependencies for hermes_api.

- get_db()        → global SessionDB instance
- get_agent()     → per-session AIAgent (cached, TTL-evicted)
- require_auth()  → optional X-Api-Key gate (off by default)
"""

import asyncio
import logging
import os
import time
from typing import Optional

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singletons — initialised in main.py lifespan
# ---------------------------------------------------------------------------

_db = None
_agent_registry: dict[str, "AIAgent"] = {}
_agent_last_used: dict[str, float] = {}

AGENT_TTL_S: int = int(os.getenv("HERMES_API_AGENT_TTL_S", "600"))


# ---------------------------------------------------------------------------
# Dependency functions
# ---------------------------------------------------------------------------

def get_db():
    """Return the global SessionDB.  Raises 503 if not yet initialised."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    return _db


def get_agent(session_id: str) -> "AIAgent":
    """Return a cached AIAgent for *session_id*, creating one if absent."""
    from run_agent import AIAgent  # deferred import — avoids heavy init at module load

    if session_id not in _agent_registry:
        _agent_registry[session_id] = AIAgent(
            platform="api",
            session_id=session_id,
            quiet_mode=True,
            enabled_toolsets=["hermes-api"],
            session_db=_db,
        )
        logger.debug("Created agent for session %s", session_id)
    _agent_last_used[session_id] = time.time()
    return _agent_registry[session_id]


async def require_auth(x_api_key: Optional[str] = Header(default=None)) -> None:
    """No-op when HERMES_API_AUTH_ENABLED != 'true'; otherwise validates X-Api-Key."""
    if os.getenv("HERMES_API_AUTH_ENABLED") != "true":
        return
    expected = os.getenv("HERMES_API_KEY", "")
    if not expected or x_api_key != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Background TTL eviction
# ---------------------------------------------------------------------------

async def _evict_stale_agents() -> None:
    """Periodically remove agents not used in AGENT_TTL_S seconds."""
    while True:
        await asyncio.sleep(60)
        cutoff = time.time() - AGENT_TTL_S
        stale = [sid for sid, t in list(_agent_last_used.items()) if t < cutoff]
        for sid in stale:
            _agent_registry.pop(sid, None)
            _agent_last_used.pop(sid, None)
        if stale:
            logger.debug("Evicted %d stale agents: %s", len(stale), stale)
