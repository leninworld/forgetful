"""Detached File policy contexts; binary content is submitted bytes only."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.file_models import File, FileSummary
from app.models.write_plugin_models import PluginWarning

FileOperation = Literal["create", "update", "delete"]


class FileMetadata(FileSummary):
    """Confirmed metadata, with the same JSON fields as File excluding data."""
    source_repo: str | None = None
    source_files: list[str] | None = None
    source_url: str | None = None
    confidence: float | None = None
    encoding_agent: str | None = None
    encoding_version: str | None = None
    agent_id: str | None = None
    agent_version: str | None = None
    agent_model: str | None = None


class FilePendingMetadata(BaseModel):
    """Core-bound candidate values, not a promise of DB constraint success.

    Explicit nulls remain representable. model_fields_set distinguishes omitted
    submitted fields; persistence retains its existing PATCH/null semantics.
    """
    filename: str | None = None
    description: str | None = None
    mime_type: str | None = None
    tags: list[str] | None = None
    project_id: int | None = None
    size_bytes: int | None = None
    source_repo: str | None = None
    source_files: list[str] | None = None
    source_url: str | None = None
    confidence: float | None = None
    encoding_agent: str | None = None
    encoding_version: str | None = None
    agent_id: str | None = None
    agent_version: str | None = None
    agent_model: str | None = None
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class FileSubmission(BaseModel):
    metadata: FilePendingMetadata
    file_id: int | None = None
    data_submitted: bool = False


class FileWriteRequest(BaseModel):
    operation: FileOperation
    user_id: UUID
    submitted: FileSubmission
    previous: FileMetadata | None
    proposed: FilePendingMetadata | None
    content: bytes | None = None
    content_availability: Literal["submitted", "not_submitted"] = "not_submitted"
    changed_fields: tuple[str, ...] = ()


class FileSavedRequest(BaseModel):
    operation: Literal["create", "update"]
    user_id: UUID
    saved: FileMetadata
    previous: FileMetadata | None
    changed_fields: tuple[str, ...]


class FileDeletedRequest(BaseModel):
    operation: Literal["delete"] = "delete"
    user_id: UUID
    deleted_file_id: int
    previous: FileMetadata


class FileWriteResult(BaseModel):
    file: File
    plugin_warnings: list[PluginWarning] = Field(default_factory=list)


class FileDeleteResult(BaseModel):
    deleted: bool
    plugin_warnings: list[PluginWarning] = Field(default_factory=list)
