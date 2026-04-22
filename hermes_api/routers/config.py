"""
Config router.

Endpoints
---------
GET    /config          effective config (secrets redacted)
PATCH  /config          set config values
POST   /config/model    switch model
"""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException

from hermes_api.deps import require_auth
from hermes_api.schemas import ConfigPatch, ModelSwitchRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Config"])

_SECRET_PATTERNS = re.compile(r"(key|token|secret|password|credential|api_key)", re.I)


def _redact(obj, depth: int = 0) -> object:
    if depth > 10:
        return obj
    if isinstance(obj, dict):
        return {
            k: "***" if _SECRET_PATTERNS.search(str(k)) else _redact(v, depth + 1)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(i, depth + 1) for i in obj]
    return obj


@router.get("/config")
async def get_config(_=Depends(require_auth)):
    """Return the effective config with sensitive values redacted."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load config: {exc}")
    return _redact(cfg)


@router.patch("/config")
async def patch_config(body: ConfigPatch, _=Depends(require_auth)):
    """Set one or more config values by dotted key path (e.g. 'model.provider')."""
    try:
        from hermes_cli.config import save_config_value
        for key, value in body.updates.items():
            save_config_value(key, value)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"status": "updated", "keys": list(body.updates.keys())}


@router.post("/config/model")
async def switch_model(body: ModelSwitchRequest, _=Depends(require_auth)):
    """Switch the active model and provider."""
    try:
        from hermes_cli.config import save_config_value
        save_config_value("model.provider", body.provider)
        save_config_value("model.name", body.model)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"status": "switched", "provider": body.provider, "model": body.model}
