"""
Skills router.

Endpoints
---------
GET    /skills                  list installed skills
POST   /skills                  create skill
GET    /skills/{name}           get skill content
PUT    /skills/{name}           update skill
DELETE /skills/{name}           delete skill
GET    /skills/hub/search?q=…   search hub
POST   /skills/hub/install      install from hub
"""

import asyncio
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from hermes_constants import get_hermes_home
from hermes_api.deps import require_auth
from hermes_api.schemas import HubInstallRequest, SkillCreate, SkillResponse, SkillUpdate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Skills"])


def _skills_dir() -> Path:
    return get_hermes_home() / "skills"


def _skill_path(name: str) -> Path:
    return _skills_dir() / name / "SKILL.md"


# ---------------------------------------------------------------------------
# Local skills CRUD
# ---------------------------------------------------------------------------

@router.get("/skills", response_model=list[SkillResponse])
async def list_skills(_=Depends(require_auth)):
    from tools.skills_tool import _find_all_skills
    loop = asyncio.get_event_loop()
    items = await loop.run_in_executor(None, _find_all_skills)
    return [
        SkillResponse(name=s["name"], description=s.get("description"))
        for s in items
    ]


@router.post("/skills", response_model=SkillResponse, status_code=201)
async def create_skill(body: SkillCreate, _=Depends(require_auth)):
    skill_file = _skill_path(body.name)
    if skill_file.exists():
        raise HTTPException(status_code=409, detail=f"Skill '{body.name}' already exists")
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(body.content, encoding="utf-8")
    return SkillResponse(name=body.name, content=body.content)


@router.get("/skills/hub/search")
async def hub_search(
    q: str = Query("", description="Search query"),
    limit: int = Query(10, ge=1, le=50),
    _=Depends(require_auth),
):
    """Search the skills hub (searches all configured sources)."""
    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(None, lambda: _do_hub_search(q, limit))
    except Exception as exc:
        logger.error("Hub search error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Hub search failed: {exc}")
    return {"query": q, "results": results}


def _do_hub_search(query: str, limit: int) -> list:
    from tools.skills_hub import create_source_router, parallel_search_sources
    sources = create_source_router()
    results, _counts, _timeouts = parallel_search_sources(sources, query=query)
    return [
        {
            "name": r.name,
            "description": r.description,
            "source": r.source,
            "identifier": r.identifier,
            "trust_level": r.trust_level,
        }
        for r in results[:limit]
    ]


@router.post("/skills/hub/install", status_code=201)
async def hub_install(body: HubInstallRequest, _=Depends(require_auth)):
    """Install a skill from the hub by name or identifier."""
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, lambda: _do_hub_install(body.skill_name))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.error("Hub install error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Hub install failed: {exc}")
    return result


def _do_hub_install(skill_name: str) -> dict:
    """Install a skill from the hub synchronously (runs in thread pool)."""
    from tools.skills_hub import create_source_router

    sources = create_source_router()
    bundle = None
    for src in sources:
        try:
            b = src.fetch(skill_name)
            if b:
                bundle = b
                break
        except Exception as exc:
            logger.debug("Fetch failed from %s: %s", src.source_id(), exc)

    if bundle is None:
        raise ValueError(f"Skill '{skill_name}' not found in any hub source")

    skills_dir = _skills_dir()
    skill_dir = skills_dir / bundle.name
    skill_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in bundle.files.items():
        dest = skill_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            dest.write_bytes(content)
        else:
            dest.write_text(content, encoding="utf-8")

    return {"name": bundle.name, "source": bundle.source, "status": "installed"}


@router.get("/skills/{name}", response_model=SkillResponse)
async def get_skill(name: str, _=Depends(require_auth)):
    skill_file = _skill_path(name)
    if not skill_file.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    content = skill_file.read_text(encoding="utf-8")
    return SkillResponse(name=name, content=content)


@router.put("/skills/{name}", response_model=SkillResponse)
async def update_skill(name: str, body: SkillUpdate, _=Depends(require_auth)):
    skill_file = _skill_path(name)
    if not skill_file.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    skill_file.write_text(body.content, encoding="utf-8")
    return SkillResponse(name=name, content=body.content)


@router.delete("/skills/{name}", status_code=204)
async def delete_skill(name: str, _=Depends(require_auth)):
    skill_dir = _skills_dir() / name
    if not skill_dir.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    shutil.rmtree(skill_dir)
