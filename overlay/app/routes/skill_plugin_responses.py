"""Skill mutation responses; stored and read skills carry no policy warnings."""

from pydantic import BaseModel, Field

from app.models.skill_models import Skill
from app.models.skill_plugin_models import SkillWriteResult
from app.models.write_plugin_models import PluginWarning


class SkillWriteResponse(Skill):
    plugin_warnings: list[PluginWarning] = Field(default_factory=list, exclude_if=lambda v: not v)


class SkillDeleteResponse(BaseModel):
    success: bool
    deleted_id: int | None = Field(default=None, exclude_if=lambda v: v is None)
    plugin_warnings: list[PluginWarning] = Field(default_factory=list, exclude_if=lambda v: not v)


def skill_write_response(result: SkillWriteResult) -> SkillWriteResponse:
    return SkillWriteResponse(**result.skill.model_dump(), plugin_warnings=result.plugin_warnings)
