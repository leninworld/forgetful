"""Write-only response extensions; stored/read memories never carry plugin warnings."""

from pydantic import BaseModel, Field

from app.models.entity_models import Entity
from app.models.file_models import File
from app.models.file_plugin_models import FileWriteResult
from app.models.memory_models import Memory, MemoryCreateResponse
from app.models.write_plugin_models import PluginWarning


class MemoryCreateWriteResponse(MemoryCreateResponse):
    plugin_warnings: list[PluginWarning] = Field(default_factory=list, exclude_if=lambda v: not v)


class MemoryUpdateResponse(Memory):
    plugin_warnings: list[PluginWarning] = Field(default_factory=list, exclude_if=lambda v: not v)


class MemoryLifecycleResponse(BaseModel):
    success: bool
    plugin_warnings: list[PluginWarning] = Field(default_factory=list, exclude_if=lambda v: not v)


class EntityWriteResponse(Entity):
    plugin_warnings: list[PluginWarning] = Field(default_factory=list, exclude_if=lambda v: not v)


class EntityDeleteResponse(BaseModel):
    success: bool
    plugin_warnings: list[PluginWarning] = Field(default_factory=list, exclude_if=lambda v: not v)


class FileWriteResponse(File):
    plugin_warnings: list[PluginWarning] = Field(default_factory=list, exclude_if=lambda v: not v)

    @classmethod
    def from_result(cls, result: FileWriteResult) -> "FileWriteResponse":
        # Internal core-validated File and runner-validated warnings only. Revalidating
        # File here would decode its binary payload again just to add a warning field.
        return cls.model_construct(
            **result.file.model_dump(), plugin_warnings=result.plugin_warnings,
        )


class FileDeleteResponse(BaseModel):
    success: bool
    plugin_warnings: list[PluginWarning] = Field(default_factory=list, exclude_if=lambda v: not v)
