"""
Tools router.

Endpoints
---------
GET    /tools           list tool schemas
POST   /tools/{name}/invoke    direct invoke (requires auth)
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from hermes_api.deps import require_auth
from hermes_api.schemas import ToolInvokeRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Tools"])


@router.get("/tools")
async def list_tools(_=Depends(require_auth)):
    """Return all available tool schemas."""
    from model_tools import get_tool_definitions
    tools = get_tool_definitions()
    return {
        "count": len(tools),
        "tools": [t.get("function", t) for t in tools],
    }


@router.post("/tools/{tool_name}/invoke")
async def invoke_tool(
    tool_name: str,
    body: ToolInvokeRequest,
    _=Depends(require_auth),
):
    """Directly invoke a tool by name (admin endpoint — use with care)."""
    from model_tools import handle_function_call, get_tool_definitions

    # Validate the tool exists
    available = {t.get("function", {}).get("name") for t in get_tool_definitions()}
    if tool_name not in available:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    loop = asyncio.get_event_loop()
    try:
        raw_result = await loop.run_in_executor(
            None,
            lambda: handle_function_call(tool_name, body.arguments),
        )
    except Exception as exc:
        logger.error("Tool invocation error [%s]: %s", tool_name, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    # handle_function_call returns a JSON string
    try:
        result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    except (json.JSONDecodeError, TypeError):
        result = raw_result

    return {"tool": tool_name, "result": result}
