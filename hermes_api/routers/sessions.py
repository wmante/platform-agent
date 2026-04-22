"""
Sessions router.

Endpoints
---------
GET  /sessions/search?q=...    semantic + FTS hybrid search
GET  /sessions/{id}/summary    LLM-generated session summary
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from hermes_api.deps import get_db, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Sessions"])


@router.get("/sessions/search")
async def search_sessions(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    _=Depends(require_auth),
):
    """Hybrid FTS + semantic search across all session messages."""
    results = db.search_messages(query=q, limit=limit, offset=offset)
    return {"query": q, "results": results, "count": len(results)}


@router.get("/sessions/{session_id}/summary")
async def get_session_summary(
    session_id: str,
    db=Depends(get_db),
    _=Depends(require_auth),
):
    """Return an LLM-generated summary of the session (or cached DB tip)."""
    row = db.get_session(session_id)
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    # Use stored compression tip if available (fast path)
    tip = db.get_compression_tip(session_id)
    if tip:
        return {"session_id": session_id, "summary": tip, "source": "cached"}

    # Fall back to generating a summary from the last N messages
    messages = db.get_messages(session_id)
    if not messages:
        return {"session_id": session_id, "summary": "", "source": "empty"}

    text_msgs = [
        m for m in messages if m.get("role") in ("user", "assistant") and m.get("content")
    ][-20:]

    transcript = "\n".join(
        f"{m['role'].upper()}: {str(m['content'])[:300]}" for m in text_msgs
    )

    loop = asyncio.get_event_loop()
    try:
        from agent.auxiliary_client import get_auxiliary_client
        aux_client = get_auxiliary_client()
        summary = await loop.run_in_executor(
            None,
            lambda: aux_client.summarize(transcript, max_words=100),
        )
    except Exception:
        # Lightweight fallback: return a truncated transcript snippet
        summary = transcript[:500] + ("..." if len(transcript) > 500 else "")

    return {"session_id": session_id, "summary": summary, "source": "generated"}
