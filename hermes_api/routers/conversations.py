"""
Conversations router.

Endpoints
---------
POST   /conversations                    create session
GET    /conversations                    list (paginated)
GET    /conversations/{id}               get with messages
DELETE /conversations/{id}               delete
POST   /conversations/{id}/messages      send (stream=true → SSE)
POST   /conversations/{id}/compress      trigger context compression
POST   /conversations/{id}/reset         end + create new session
"""

import asyncio
import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from hermes_api.deps import get_agent, get_db, require_auth
from hermes_api.schemas import (
    MessageRequest,
    MessageResponse,
    SessionCreate,
    SessionResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Conversations"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_to_response(row: dict, db) -> SessionResponse:
    session_id = row.get("session_id") or row.get("id", "")
    msg_count = 0
    try:
        msg_count = db.message_count(session_id)
    except Exception:
        pass
    return SessionResponse(
        id=session_id,
        source=row.get("source", ""),
        title=row.get("title"),
        created_at=row.get("created_at"),
        message_count=msg_count,
        model=row.get("model"),
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.post("/conversations", response_model=SessionResponse, status_code=201)
async def create_conversation(
    body: SessionCreate,
    db=Depends(get_db),
    _=Depends(require_auth),
):
    session_id = uuid.uuid4().hex
    db.create_session(
        session_id=session_id,
        source=body.platform,
        model=body.model,
        user_id=body.user_id,
    )
    if body.title:
        db.set_session_title(session_id, body.title)
    row = db.get_session(session_id) or {}
    row.setdefault("session_id", session_id)
    row.setdefault("source", body.platform)
    row.setdefault("model", body.model)
    return _session_to_response(row, db)


@router.get("/conversations", response_model=list[SessionResponse])
async def list_conversations(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    _=Depends(require_auth),
):
    rows = db.list_sessions_rich(limit=limit, offset=offset)
    return [_session_to_response(r, db) for r in rows]


@router.get("/conversations/{session_id}")
async def get_conversation(
    session_id: str,
    db=Depends(get_db),
    _=Depends(require_auth),
):
    row = db.get_session(session_id)
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = db.get_messages(session_id)
    resp = _session_to_response(row, db)
    return {**resp.model_dump(), "messages": messages}


@router.delete("/conversations/{session_id}", status_code=204)
async def delete_conversation(
    session_id: str,
    db=Depends(get_db),
    _=Depends(require_auth),
):
    deleted = db.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")


# ---------------------------------------------------------------------------
# Send message (non-streaming and SSE streaming)
# ---------------------------------------------------------------------------

@router.post("/conversations/{session_id}/messages")
async def send_message(
    session_id: str,
    body: MessageRequest,
    db=Depends(get_db),
    _=Depends(require_auth),
):
    # Ensure session exists
    if not db.get_session(session_id):
        raise HTTPException(status_code=404, detail="Conversation not found")

    if body.stream:
        return await _stream_response(session_id, body.content, db)
    else:
        return await _sync_response(session_id, body.content, db)


async def _sync_response(session_id: str, content: str, db) -> MessageResponse:
    """Run agent synchronously in thread pool and return plain JSON."""
    loop = asyncio.get_event_loop()
    agent = get_agent(session_id)
    try:
        result = await loop.run_in_executor(
            None,
            lambda: agent.run_conversation(content),
        )
    except Exception as exc:
        logger.error("Agent error for session %s: %s", session_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    final = result.get("final_response", "") if isinstance(result, dict) else str(result)
    # Count tool calls in the result messages
    tool_count = sum(
        1
        for m in (result.get("messages", []) if isinstance(result, dict) else [])
        if m.get("role") == "tool"
    )
    return MessageResponse(
        session_id=session_id,
        content=final,
        tool_calls_count=tool_count,
    )


async def _stream_response(session_id: str, content: str, db) -> StreamingResponse:
    """Run agent in thread pool, forward events over SSE."""
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    agent = get_agent(session_id)

    # Wire callbacks — called from the executor thread, safe via call_soon_threadsafe
    def _put(event: dict):
        loop.call_soon_threadsafe(queue.put_nowait, event)

    agent.stream_delta_callback = lambda text: _put({"type": "delta", "data": text})
    agent.tool_start_callback = lambda name, args: _put(
        {"type": "tool_call", "data": {"name": name, "args": args}}
    )
    agent.tool_complete_callback = lambda name, result: _put(
        {"type": "tool_result", "data": {"name": name, "result": str(result)[:500]}}
    )

    async def _run():
        try:
            result = await loop.run_in_executor(
                None, lambda: agent.run_conversation(content)
            )
            final = result.get("final_response", "") if isinstance(result, dict) else str(result)
            await queue.put({"type": "done", "data": final})
        except Exception as exc:
            logger.error("SSE agent error for session %s: %s", session_id, exc)
            await queue.put({"type": "error", "data": str(exc)})
        finally:
            agent.stream_delta_callback = None
            agent.tool_start_callback = None
            agent.tool_complete_callback = None

    asyncio.create_task(_run())

    async def event_generator():
        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] in ("done", "error"):
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Compress and reset
# ---------------------------------------------------------------------------

@router.post("/conversations/{session_id}/compress", status_code=202)
async def compress_conversation(
    session_id: str,
    db=Depends(get_db),
    _=Depends(require_auth),
):
    if not db.get_session(session_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    loop = asyncio.get_event_loop()
    agent = get_agent(session_id)
    try:
        await loop.run_in_executor(None, lambda: agent.context_compressor.compress(
            agent._session_messages
        ))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Compression failed: {exc}")
    return {"status": "compressed", "session_id": session_id}


@router.post("/conversations/{session_id}/reset", response_model=SessionResponse, status_code=201)
async def reset_conversation(
    session_id: str,
    db=Depends(get_db),
    _=Depends(require_auth),
):
    if not db.get_session(session_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    db.end_session(session_id, end_reason="api_reset")

    # Evict cached agent so next request gets a fresh one
    from hermes_api import deps as _deps
    _deps._agent_registry.pop(session_id, None)
    _deps._agent_last_used.pop(session_id, None)

    # Create successor session
    new_id = uuid.uuid4().hex
    db.create_session(session_id=new_id, source="api", parent_session_id=session_id)
    row = db.get_session(new_id) or {}
    row.setdefault("session_id", new_id)
    row.setdefault("source", "api")
    return _session_to_response(row, db)
