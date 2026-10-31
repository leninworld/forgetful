"""Detached skill policy contexts and transient mutation results."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.skill_models import Skill, SkillCreate, SkillUpdate
from app.models.write_plugin_models import PluginWarning

SkillOperation = Literal["create", "update", "delete"]


class SkillWriteRequest(BaseModel):
    operation: Literal["create", "update"]
    user_id: UUID
    submitted: SkillCreate | SkillUpdate
    previous: Skill | None
    proposed: SkillCreate


class SkillDeleteRequest(BaseModel):
    operation: Literal["delete"] = "delete"
    user_id: UUID
    skill_id: int
    previous: Skill
    proposed_deleted: Literal[True] = True


class SkillSavedRequest(BaseModel):
    operation: SkillOperation
    user_id: UUID
    previous: Skill | None
    saved: Skill | None
    deleted: bool = False


class SkillWriteResult(BaseModel):
    skill: Skill
    plugin_warnings: list[PluginWarning] = Field(default_factory=list)


class SkillDeleteResult(BaseModel):
    success: bool
    plugin_warnings: list[PluginWarning] = Field(default_factory=list)
