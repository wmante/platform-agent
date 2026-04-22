"""
Platforms router.

Endpoints
---------
GET    /platforms                        list platform connection status
POST   /platforms/{platform}/send        send a message via platform adapter
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from hermes_api.deps import require_auth
from hermes_api.schemas import PlatformSendRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Platforms"])


@router.get("/platforms")
async def list_platforms(_=Depends(require_auth)):
    """Return connection status for all configured platforms."""
    try:
        from gateway.config import load_gateway_config
        cfg = load_gateway_config()
        platforms = {}
        for platform, pconfig in cfg.platforms.items():
            platforms[platform.value] = {
                "enabled": pconfig.enabled,
                "has_token": bool(pconfig.token or pconfig.api_key),
                "home_channel": (
                    pconfig.home_channel.to_dict() if pconfig.home_channel else None
                ),
            }
        connected = [p for p, s in platforms.items() if s["enabled"]]
    except Exception as exc:
        logger.warning("Could not load gateway config: %s", exc)
        platforms = {}
        connected = []

    return {"connected": connected, "platforms": platforms}


@router.post("/platforms/{platform_name}/send", status_code=202)
async def send_platform_message(
    platform_name: str,
    body: PlatformSendRequest,
    _=Depends(require_auth),
):
    """Send a message to a specific chat via the platform adapter.

    Requires the gateway to be running and the platform to be connected.
    """
    try:
        from model_tools import handle_function_call
        import json

        result_raw = handle_function_call(
            "send_message",
            {
                "platform": platform_name,
                "chat_id": body.chat_id,
                "message": body.message,
            },
        )
        result = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
        if isinstance(result, dict) and result.get("error"):
            raise HTTPException(status_code=502, detail=result["error"])
        return {"status": "sent", "platform": platform_name, "chat_id": body.chat_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Platform send error [%s]: %s", platform_name, exc)
        raise HTTPException(status_code=502, detail=str(exc))
