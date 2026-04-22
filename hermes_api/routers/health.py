"""Health, readiness, and Prometheus metrics endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from hermes_api.deps import get_db

router = APIRouter(tags=["Health"])


@router.get("/v1/health", summary="Liveness probe — process-only check")
async def liveness():
    return {"status": "ok"}


@router.get("/v1/ready", summary="Readiness probe — DB connectivity check")
async def readiness(db=Depends(get_db)):
    try:
        db.session_count()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")
    return {"status": "ready"}


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
async def metrics():
    """Prometheus text-format metrics (placeholder — wire custom counters here)."""
    return "# Hermes Agent API metrics\n# TYPE hermes_api_up gauge\nhermes_api_up 1\n"
