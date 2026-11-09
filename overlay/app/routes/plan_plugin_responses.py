"""Write-only Plan and Task payloads; reads retain their existing models."""

from pydantic import BaseModel, Field

from app.models.plan_models import Criterion, Plan, Task, TaskDependency
from app.models.write_plugin_models import PluginWarning


class PlanWriteResponse(Plan):
    plugin_warnings: list[PluginWarning] = Field(default_factory=list, exclude_if=lambda v: not v)


class TaskTransitionResponse(Task):
    plugin_warnings: list[PluginWarning] = Field(default_factory=list, exclude_if=lambda v: not v)


class PlanningDeleteResponse(BaseModel):
    success: bool
    plugin_warnings: list[PluginWarning] = Field(default_factory=list, exclude_if=lambda v: not v)


class CriterionWriteResponse(Criterion):
    plugin_warnings: list[PluginWarning] = Field(default_factory=list, exclude_if=lambda v: not v)


class DependencyWriteResponse(TaskDependency):
    plugin_warnings: list[PluginWarning] = Field(default_factory=list, exclude_if=lambda v: not v)
