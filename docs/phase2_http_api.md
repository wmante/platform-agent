# Phase 2 — HTTP REST API

## Overview

Add a `hermes_api/` FastAPI application that exposes a full versioned REST
API over the agent, sessions, skills, memory, config, cron, tools, and
platform surfaces. SSE streaming is supported on the message-send endpoint.
Auth is stubbed (off by default, switchable via env var). The API registers
as the `"api"` platform so it integrates with the existing platform model.

**Warp plan ID:** `83929ef0-ead7-4b45-8827-3de8f5f661df`

---

## Prerequisites

- Phase 1 complete and merged
- `hermes-agent[web]` extras installed (`fastapi`, `uvicorn`)
- `docker/compose.dev.yaml` from Phase 1 running for local Postgres

---

## Context: Current State

| File | Role |
|------|------|
| `run_agent.py` | `AIAgent` class — synchronous agent loop |
| `gateway/config.py` | `Platform` enum + `PLATFORM_HINTS` dict |
| `toolsets.py` | Toolset definitions |
| `hermes_cli/main.py` | All CLI subcommands |

The `AIAgent.run_conversation()` method is **synchronous**. Streaming is
achieved by injecting callbacks. The API layer will run this in a thread pool
executor and forward events over SSE.

`fastapi>=0.104.0` and `uvicorn[standard]` are already declared in the `web`
optional dep group. No `hermes_api/` package exists yet.

---

## Package structure

```
hermes_api/
├── __init__.py
├── main.py          — FastAPI app, lifespan, uvicorn entry point
├── deps.py          — get_db(), get_agent(), require_auth()
├── middleware.py     — request-id, JSON logging, ddtrace guard
├── errors.py         — exception → HTTP mapping
├── schemas.py        — shared Pydantic models
└── routers/
    ├── __init__.py
    ├── conversations.py
    ├── sessions.py
    ├── skills.py
    ├── memory.py
    ├── config.py
    ├── cron.py
    ├── tools.py
    ├── platforms.py
    └── health.py
```

---

## Step-by-step implementation

### Step 1 — Platform and toolset registration

**`gateway/config.py`** — add to the `Platform` class:

```python
API = "api"
```

Add to `PLATFORM_HINTS`:

```python
"api": {
    "format": "markdown",
    "emoji_ok": True,
    "max_message_length": None,
},
```

**`toolsets.py`** — add a `hermes-api` toolset entry. Start with the same
tool set as `hermes-cli` so the API surface is identical to the CLI.

**Commit after this step.**

---

### Step 2 — `hermes_api/schemas.py`

Define Pydantic models for all request/response bodies. Key models:

```python
class MessageRequest(BaseModel):
    content: str
    stream: bool = False
    user_id: str = "default"

class MessageResponse(BaseModel):
    session_id: str
    content: str
    tool_calls_count: int = 0

class SessionCreate(BaseModel):
    platform: str = "api"
    user_id: str = "default"
    model: str | None = None

class SessionResponse(BaseModel):
    id: str
    source: str
    title: str | None
    created_at: float
    message_count: int
    # ... mirror all session columns

class SSEEvent(BaseModel):
    type: Literal["delta", "tool_call", "tool_result", "done", "error"]
    data: str | dict
```

---

### Step 3 — `hermes_api/errors.py`

```python
from fastapi import Request
from fastapi.responses import JSONResponse

async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})

async def key_error_handler(request: Request, exc: KeyError):
    return JSONResponse(status_code=404, content={"detail": "Not found"})

async def unhandled_handler(request: Request, exc: Exception):
    request_id = request.headers.get("X-Request-Id", "unknown")
    # log with ERROR level, do not expose internal detail
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": request_id},
    )
```

Register all three handlers in `main.py` via `app.add_exception_handler()`.

---

### Step 4 — `hermes_api/middleware.py`

```python
import uuid
from starlette.middleware.base import BaseHTTPMiddleware

class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response
```

For Datadog APM, guard the `ddtrace` import:

```python
try:
    from ddtrace.contrib.asgi import TraceMiddleware
    _ddtrace_available = True
except ImportError:
    _ddtrace_available = False
```

Add `TraceMiddleware` only when `_ddtrace_available` is `True`.

---

### Step 5 — `hermes_api/deps.py`

```python
import os
from hermes_state import SessionDB
from run_agent import AIAgent

_db: SessionDB | None = None
_agent_registry: dict[str, AIAgent] = {}

def get_db() -> SessionDB:
    return _db

def get_agent(session_id: str) -> AIAgent:
    """Return cached agent or create one for the session."""
    if session_id not in _agent_registry:
        _agent_registry[session_id] = AIAgent(platform="api")
    return _agent_registry[session_id]

async def require_auth(x_api_key: str | None = Header(default=None)):
    if os.getenv("HERMES_API_AUTH_ENABLED") != "true":
        return
    expected = os.getenv("HERMES_API_KEY")
    if not expected or x_api_key != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
```

Agent TTL eviction: in `main.py` lifespan, start a background task that
removes agents from `_agent_registry` that haven't been used in
`HERMES_API_AGENT_TTL_S` seconds (default `600`). Store last-used timestamps
in a parallel dict.

---

### Step 6 — `hermes_api/main.py`

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from hermes_api import deps
from hermes_api.middleware import RequestIdMiddleware
from hermes_api.errors import value_error_handler, key_error_handler, unhandled_handler
from hermes_api.routers import conversations, sessions, skills, memory, config, cron, tools, platforms, health
from hermes_state import SessionDB

@asynccontextmanager
async def lifespan(app: FastAPI):
    deps._db = SessionDB()
    # start agent TTL eviction background task
    yield
    deps._db.close()

app = FastAPI(title="Hermes Agent API", version="1.0.0", lifespan=lifespan)
app.add_middleware(RequestIdMiddleware)
app.add_exception_handler(ValueError, value_error_handler)
app.add_exception_handler(KeyError, key_error_handler)
app.add_exception_handler(Exception, unhandled_handler)

app.include_router(conversations.router, prefix="/v1")
app.include_router(sessions.router, prefix="/v1")
app.include_router(skills.router, prefix="/v1")
app.include_router(memory.router, prefix="/v1")
app.include_router(config.router, prefix="/v1")
app.include_router(cron.router, prefix="/v1")
app.include_router(tools.router, prefix="/v1")
app.include_router(platforms.router, prefix="/v1")
app.include_router(health.router)  # /v1/health and /metrics (no /v1 prefix on /metrics)

def run():
    import uvicorn, os
    uvicorn.run(
        "hermes_api.main:app",
        host=os.getenv("HERMES_API_HOST", "0.0.0.0"),
        port=int(os.getenv("HERMES_API_PORT", "8000")),
        log_config=None,  # use hermes_logging structured JSON
    )
```

**Add to `pyproject.toml`:**

```toml
[project.scripts]
hermes-api = "hermes_api.main:run"
```

**Add `hermes_api` to `packages.find.include` in `pyproject.toml`.**

**Commit after this step.**

---

### Step 7 — `routers/health.py`

```python
from fastapi import APIRouter, Depends
from hermes_api.deps import get_db

router = APIRouter(tags=["Health"])

@router.get("/v1/health")
async def liveness():
    return {"status": "ok"}

@router.get("/v1/ready")
async def readiness(db=Depends(get_db)):
    # ping DB; raise 503 if unreachable
    try:
        db.session_count()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "ready"}

@router.get("/metrics")
async def metrics():
    # return Prometheus text format; wire custom counters here
    return PlainTextResponse("# placeholder\n")
```

---

### Step 8 — `routers/conversations.py` (core router)

#### Non-streaming send

```python
@router.post("/conversations/{session_id}/messages", response_model=MessageResponse)
async def send_message(
    session_id: str,
    body: MessageRequest,
    db=Depends(get_db),
    _=Depends(require_auth),
):
    agent = get_agent(session_id)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: agent.run_conversation(body.content)
    )
    return MessageResponse(session_id=session_id, content=result["final_response"])
```

#### SSE streaming

```python
@router.post("/conversations/{session_id}/messages")
async def send_message_stream(
    session_id: str,
    body: MessageRequest,
    db=Depends(get_db),
    _=Depends(require_auth),
):
    if not body.stream:
        # fall through to non-streaming handler above
        ...

    async def event_generator():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def on_delta(text: str):
            queue.put_nowait({"type": "delta", "data": text})

        def on_tool_call(name: str, args: dict):
            queue.put_nowait({"type": "tool_call", "data": {"name": name, "args": args}})

        def on_tool_result(name: str, result: str):
            queue.put_nowait({"type": "tool_result", "data": {"name": name, "result": result}})

        async def run_agent():
            agent = get_agent(session_id)
            try:
                result = await loop.run_in_executor(
                    None,
                    lambda: agent.run_conversation(
                        body.content,
                        callbacks={"on_delta": on_delta, "on_tool_call": on_tool_call, "on_tool_result": on_tool_result},
                    )
                )
                queue.put_nowait({"type": "done", "data": result["final_response"]})
            except Exception as exc:
                queue.put_nowait({"type": "error", "data": str(exc)})

        asyncio.create_task(run_agent())

        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] in ("done", "error"):
                break

    return EventSourceResponse(event_generator())
```

Use `sse-starlette` package for `EventSourceResponse`, or implement it
directly using `StreamingResponse` with `media_type="text/event-stream"`.

**Note:** `AIAgent` must expose callback injection for streaming. If it
doesn't yet, a minimal patch to `run_agent.py` is needed to accept
`on_delta`, `on_tool_call`, and `on_tool_result` callables and call them at
the appropriate points in the loop.

---

### Step 9 — Remaining routers

Implement the full route surface from the original plan. Each router follows
the same pattern: `Depends(get_db)`, `Depends(require_auth)`, calls into the
appropriate backend class or `hermes_cli` logic.

Full route table:

```
POST   /v1/conversations                  → create session + return SessionResponse
GET    /v1/conversations                  → list_sessions_rich (paginated, ?user_id)
GET    /v1/conversations/{id}             → get_session + get_messages
DELETE /v1/conversations/{id}             → delete_session
POST   /v1/conversations/{id}/messages    → send (stream=true → SSE)
POST   /v1/conversations/{id}/compress    → trigger context compression
POST   /v1/conversations/{id}/reset       → end + create new session

GET    /v1/sessions/search?q=…            → search_messages (hybrid)
GET    /v1/sessions/{id}/summary          → LLM-generated summary of session

GET    /v1/skills                         → list installed skills
POST   /v1/skills                         → create skill
GET    /v1/skills/{name}                  → get skill content
PUT    /v1/skills/{name}                  → update skill
DELETE /v1/skills/{name}                  → delete skill
GET    /v1/skills/hub/search?q=…          → hub search (wraps existing hub logic)
POST   /v1/skills/hub/install             → install from hub

GET    /v1/memory?kind=…                  → list memory entries
POST   /v1/memory                         → create memory entry
PUT    /v1/memory/{id}                    → update
DELETE /v1/memory/{id}                    → delete

GET    /v1/config                         → effective config (secrets redacted)
PATCH  /v1/config                         → set config values
POST   /v1/config/model                   → switch model

GET    /v1/cron                           → list cron jobs
POST   /v1/cron                           → create job
PUT    /v1/cron/{id}                      → update job
DELETE /v1/cron/{id}                      → delete job

GET    /v1/tools                          → list tool schemas
POST   /v1/tools/{name}/invoke            → direct invoke (requires auth)

GET    /v1/platforms                      → gateway connection status
POST   /v1/platforms/{platform}/send      → send_message via platform adapter
```

**Commit after this step.**

---

### Step 10 — Update Docker Compose

Extend `docker/compose.dev.yaml` to add the API service:

```yaml
  hermes-api:
    build: .
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      HERMES_DB_URL: postgresql+asyncpg://hermes:hermes@postgres:5432/hermes
      HERMES_API_PORT: "8000"
    ports:
      - "8000:8000"
    volumes:
      - hermes-home:/root/.hermes

volumes:
  pgdata:
  hermes-home:
```

---

### Step 11 — Tests

Create `tests/hermes_api/` with router tests:

```python
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import MagicMock, patch

@pytest.fixture
async def client():
    from hermes_api.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/v1/health")
    assert r.status_code == 200

@pytest.mark.asyncio
async def test_send_message_non_streaming(client):
    with patch("hermes_api.deps.get_agent") as mock_get_agent:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "hello"}
        mock_get_agent.return_value = mock_agent
        r = await client.post("/v1/conversations/test-session/messages",
                              json={"content": "hi", "stream": False})
        assert r.status_code == 200
        assert r.json()["content"] == "hello"
```

Test SSE with a helper that reads the event stream:

```python
async def collect_sse(response) -> list[dict]:
    events = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events
```

Run with:
```bash
scripts/run_tests.sh tests/hermes_api/
```

**Commit after this step.**

---

### Step 12 — Final validation

```bash
# Start local stack
docker compose -f docker/compose.dev.yaml up -d

# Start API server
HERMES_DB_URL="postgresql+asyncpg://hermes:hermes@localhost:5432/hermes" \
  hermes-api

# Check OpenAPI
curl http://localhost:8000/docs   # should render UI
curl http://localhost:8000/v1/health  # {"status": "ok"}

# Run tests
scripts/run_tests.sh tests/hermes_api/
```

---

## Commit checklist

1. Platform + toolset registration
2. `schemas.py`
3. `errors.py` + `middleware.py`
4. `deps.py`
5. `main.py` + `pyproject.toml` entry point
6. `health.py` router
7. `conversations.py` router (non-streaming + SSE)
8. Remaining routers
9. Docker Compose update
10. Tests
