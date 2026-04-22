"""
Memory router.

Entries are identified by zero-based index within their kind.

Endpoints
---------
GET    /memory?kind=memory|user_profile    list entries
POST   /memory                             add entry
PUT    /memory/{id}                        update entry at index
DELETE /memory/{id}                        remove entry at index
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from hermes_api.deps import require_auth
from hermes_api.schemas import MemoryEntryCreate, MemoryEntryResponse, MemoryEntryUpdate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Memory"])


def _load_store():
    from tools.memory_tool import MemoryStore
    store = MemoryStore()
    store.load_from_disk()
    return store


def _entries_for_kind(store, kind: str) -> list:
    if kind == "user_profile":
        return store.user_entries
    return store.memory_entries


@router.get("/memory", response_model=list[MemoryEntryResponse])
async def list_memory(
    kind: Literal["memory", "user_profile"] = Query("memory"),
    _=Depends(require_auth),
):
    store = _load_store()
    entries = _entries_for_kind(store, kind)
    return [MemoryEntryResponse(id=i, kind=kind, content=e) for i, e in enumerate(entries)]


@router.post("/memory", response_model=MemoryEntryResponse, status_code=201)
async def add_memory(body: MemoryEntryCreate, _=Depends(require_auth)):
    from tools.memory_tool import _scan_memory_content

    blocked = _scan_memory_content(body.content)
    if blocked:
        raise HTTPException(status_code=422, detail=blocked)

    store = _load_store()
    entries = _entries_for_kind(store, body.kind)
    entries.append(body.content)

    if body.kind == "user_profile":
        store.user_entries = entries
    else:
        store.memory_entries = entries

    store.save_to_disk("user" if body.kind == "user_profile" else "memory")
    new_id = len(entries) - 1
    return MemoryEntryResponse(id=new_id, kind=body.kind, content=body.content)


@router.put("/memory/{entry_id}", response_model=MemoryEntryResponse)
async def update_memory(
    entry_id: int,
    body: MemoryEntryUpdate,
    _=Depends(require_auth),
):
    from tools.memory_tool import _scan_memory_content

    # We need the kind to know which list to update.
    # Convention: caller must append ?kind= or we default to "memory".
    # For simplicity, try memory first then user_profile.
    blocked = _scan_memory_content(body.content)
    if blocked:
        raise HTTPException(status_code=422, detail=blocked)

    store = _load_store()
    for kind, entries in [("memory", store.memory_entries), ("user_profile", store.user_entries)]:
        if entry_id < len(entries):
            entries[entry_id] = body.content
            if kind == "user_profile":
                store.user_entries = entries
            else:
                store.memory_entries = entries
            store.save_to_disk("user" if kind == "user_profile" else "memory")
            return MemoryEntryResponse(id=entry_id, kind=kind, content=body.content)

    raise HTTPException(status_code=404, detail=f"Memory entry {entry_id} not found")


@router.delete("/memory/{entry_id}", status_code=204)
async def delete_memory(
    entry_id: int,
    kind: Literal["memory", "user_profile"] = Query("memory"),
    _=Depends(require_auth),
):
    store = _load_store()
    entries = list(_entries_for_kind(store, kind))
    if entry_id >= len(entries):
        raise HTTPException(status_code=404, detail=f"Memory entry {entry_id} not found")

    entries.pop(entry_id)
    if kind == "user_profile":
        store.user_entries = entries
    else:
        store.memory_entries = entries
    store.save_to_disk("user" if kind == "user_profile" else "memory")
