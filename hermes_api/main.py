"""
hermes_api.main — FastAPI application factory and uvicorn entry point.

Usage:
    hermes-api                   # via pyproject.toml script
    uvicorn hermes_api.main:app  # directly
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from hermes_api import deps
from hermes_api.errors import key_error_handler, unhandled_handler, value_error_handler
from hermes_api.middleware import RequestIdMiddleware, apply_optional_ddtrace
from hermes_api.routers import (
    config,
    conversations,
    cron,
    health,
    memory,
    platforms,
    sessions,
    skills,
    tools,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise DB pool and start background maintenance tasks."""
    from hermes_state import SessionDB

    deps._db = SessionDB()

    eviction_task = asyncio.create_task(deps._evict_stale_agents())
    try:
        yield
    finally:
        eviction_task.cancel()
        try:
            await eviction_task
        except asyncio.CancelledError:
            pass
        if deps._db is not None:
            deps._db.close()


app = FastAPI(
    title="Hermes Agent API",
    version="1.0.0",
    description=(
        "Management REST API for Hermes Agent — sessions, skills, memory, "
        "config, cron, tools, and platform surfaces.  "
        "SSE streaming supported on the message-send endpoint."
    ),
    lifespan=lifespan,
)

# Middleware
app.add_middleware(RequestIdMiddleware)
apply_optional_ddtrace(app)

# Exception handlers
app.add_exception_handler(ValueError, value_error_handler)
app.add_exception_handler(KeyError, key_error_handler)
app.add_exception_handler(Exception, unhandled_handler)

# Routers
app.include_router(health.router)           # /v1/health, /v1/ready, /metrics
app.include_router(conversations.router, prefix="/v1")
app.include_router(sessions.router, prefix="/v1")
app.include_router(skills.router, prefix="/v1")
app.include_router(memory.router, prefix="/v1")
app.include_router(config.router, prefix="/v1")
app.include_router(cron.router, prefix="/v1")
app.include_router(tools.router, prefix="/v1")
app.include_router(platforms.router, prefix="/v1")


def run() -> None:
    """uvicorn entry point (used by `hermes-api` CLI script)."""
    import uvicorn

    uvicorn.run(
        "hermes_api.main:app",
        host=os.getenv("HERMES_API_HOST", "0.0.0.0"),
        port=int(os.getenv("HERMES_API_PORT", "8000")),
        log_config=None,
    )
