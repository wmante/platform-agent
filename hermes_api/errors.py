"""Domain exception → HTTP status mapping for hermes_api."""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


async def key_error_handler(request: Request, exc: KeyError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Not found"})


async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = request.headers.get("X-Request-Id", "unknown")
    logger.error(
        "Unhandled exception [request_id=%s] %s: %s",
        request_id,
        type(exc).__name__,
        exc,
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": request_id},
    )
