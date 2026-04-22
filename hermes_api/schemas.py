"""Pydantic request/response models for hermes_api."""

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

class SessionCreate(BaseModel):
    platform: str = "api"
    user_id: str = "default"
    model: Optional[str] = None
    title: Optional[str] = None


class SessionResponse(BaseModel):
    id: str
    source: str
    title: Optional[str]
    created_at: Optional[Any]
    message_count: int
    model: Optional[str] = None


class MessageRequest(BaseModel):
    content: str
    stream: bool = False
    user_id: str = "default"


class MessageResponse(BaseModel):
    session_id: str
    content: str
    tool_calls_count: int = 0


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

class SkillCreate(BaseModel):
    name: str = Field(..., description="Skill directory name (slug)")
    content: str = Field(..., description="Full SKILL.md content")


class SkillUpdate(BaseModel):
    content: str


class SkillResponse(BaseModel):
    name: str
    description: Optional[str] = None
    content: Optional[str] = None


class HubInstallRequest(BaseModel):
    skill_name: str


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class MemoryEntryCreate(BaseModel):
    kind: Literal["memory", "user_profile"] = "memory"
    content: str


class MemoryEntryUpdate(BaseModel):
    content: str


class MemoryEntryResponse(BaseModel):
    id: int
    kind: str
    content: str


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class ConfigPatch(BaseModel):
    updates: dict[str, Any]


class ModelSwitchRequest(BaseModel):
    provider: str
    model: str


# ---------------------------------------------------------------------------
# Cron
# ---------------------------------------------------------------------------

class CronCreate(BaseModel):
    prompt: str
    schedule: str
    name: Optional[str] = None
    deliver: Optional[str] = None
    repeat: Optional[int] = None


class CronUpdate(BaseModel):
    prompt: Optional[str] = None
    schedule: Optional[str] = None
    name: Optional[str] = None
    deliver: Optional[str] = None
    enabled: Optional[bool] = None


class CronResponse(BaseModel):
    job_id: str
    name: str
    schedule: Optional[str] = None
    enabled: bool
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class ToolInvokeRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Platforms
# ---------------------------------------------------------------------------

class PlatformSendRequest(BaseModel):
    chat_id: str
    message: str
