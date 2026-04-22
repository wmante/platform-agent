"""ASGI middleware for hermes_api."""

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

try:
    from ddtrace.contrib.asgi import TraceMiddleware as _DDTraceMiddleware
    _ddtrace_available = True
except ImportError:
    _DDTraceMiddleware = None
    _ddtrace_available = False


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach / propagate a request-id header on every response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response


def apply_optional_ddtrace(app):
    """Wrap *app* with Datadog APM middleware when ddtrace is installed."""
    if _ddtrace_available:
        return _DDTraceMiddleware(app)
    return app
