"""File Service - Business logic for file operations

This service implements functionality for managing binary files:
    - CRUD operations (create, read, update, delete)
    - Filtering and search
    - Project association
    - Memory/entity linking (via association tables)
"""
import base64
from typing import TYPE_CHECKING
from uuid import UUID

from app.config.logging_config import logging
from app.config.settings import settings
from app.exceptions import NotFoundError
from app.models.activity_models import (
    ActionType,
    ActivityEvent,
    ActorType,
    EntityType,
)
from app.models.file_models import (
    File,
    FileCreate,
    FileSummary,
    FileUpdate,
)
from app.models.file_plugin_models import (
    FileDeletedRequest, FileDeleteResult, FileMetadata, FilePendingMetadata,
    FileSavedRequest, FileSubmission, FileWriteRequest, FileWriteResult,
)
from app.protocols.file_protocol import FileRepository
from app.protocols.project_protocol import ProjectRepository
from app.utils.provenance import (
    apply_provenance_defaults,
    apply_provenance_defaults_for_update,
)
from app.utils.pydantic_helper import get_changed_fields

if TYPE_CHECKING:
    from app.events import EventBus
    from app.services.file_write_plugin_service import FileWritePluginService

logger = logging.getLogger(__name__)


class FileService:
    """Service layer for file operations

    Handles business logic for creating, updating, querying, and deleting files.
    Uses repository protocol for data access.
    """

    def __init__(
        self,
        file_repo: FileRepository,
        event_bus: "EventBus | None" = None,
        file_plugin: "FileWritePluginService | None" = None,
        project_repo: ProjectRepository | None = None,
    ):
        """Initialize with repository protocol (not concrete implementation)

        Args:
            file_repo: File repository implementing the protocol
            event_bus: Optional event bus for activity tracking
        """
        if file_plugin is not None and project_repo is None:
            raise ValueError("File Plugin requires a project repository")
        self._file_plugin = file_plugin
        self._project_repo = project_repo
        self.file_repo = file_repo
        self._event_bus = event_bus
        logger.info("File service initialized")

    async def _emit_event(
        self,
        user_id: UUID,
        entity_type: EntityType,
        entity_id: int,
        action: ActionType,
        snapshot: dict,
        changes: dict | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Emit an activity event to the event bus.

        This is a no-op if no event bus is configured.
        """
        if self._event_bus is None:
            return

        event = ActivityEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            changes=changes,
            snapshot=snapshot,
            actor=ActorType.USER,
            metadata=metadata,
            user_id=str(user_id),
        )

        await self._event_bus.emit(event)

    def _snapshot_without_data(self, file: File | FileMetadata) -> dict:
        """Create a snapshot dict excluding the base64 data field to avoid bloating events."""
        snapshot = file.model_dump(mode="json")
        snapshot.pop("data", None)
        return snapshot

    @staticmethod
    def _metadata(file: File) -> FileMetadata:
        return FileMetadata.model_validate(file.model_dump(exclude={"data"}))

    @staticmethod
    def _submission(data: FileCreate | FileUpdate, file_id: int | None = None) -> FileSubmission:
        return FileSubmission(
            metadata=FilePendingMetadata.model_validate(
                data.model_dump(exclude_unset=True, exclude={"data"}),
            ), file_id=file_id, data_submitted="data" in data.model_fields_set,
        )

    async def _check_project(self, user_id: UUID, project_id: int | None) -> None:
        if project_id is not None:
            # Construction guarantees this dependency for a configured runner.
            project = await self._project_repo.get_project_by_id(user_id, project_id)
            if project is None:
                raise NotFoundError(f"Project {project_id} not found")

    async def create_file(self, user_id: UUID, file_data: FileCreate) -> FileWriteResult:
        """Create new file

        Args:
            user_id: User ID for ownership
            file_data: FileCreate with filename, description, data (base64), mime_type, tags

        Returns:
            Created File with generated ID, size_bytes, and timestamps
        """
        logger.info(
            "creating file",
            extra={
                "filename": file_data.filename[:50],
                "mime_type": file_data.mime_type,
                "user_id": str(user_id),
            },
        )
        submitted = file_data.model_copy(deep=True)
        core = apply_provenance_defaults(file_data.model_copy(deep=True))
        warnings = []
        plugin = self._file_plugin
        if plugin and plugin.has_before("create"):
            await self._check_project(user_id, core.project_id)
            content = base64.b64decode(core.data)
            proposed = core.model_dump(exclude={"data"})
            proposed["size_bytes"] = len(content)
            warnings = await plugin.before_save(FileWriteRequest(
                operation="create", user_id=user_id,
                submitted=self._submission(submitted), previous=None,
                proposed=FilePendingMetadata.model_validate(proposed), content=content,
                content_availability="submitted",
                changed_fields=tuple(FileCreate.model_fields),
            ))
        file = await self.file_repo.create_file(user_id=user_id, file_data=core)
        logger.info(
            "file created",
            extra={
                "file_id": file.id,
                "user_id": str(user_id),
            },
        )
        await self._emit_event(user_id=user_id, entity_type=EntityType.FILE,
                               entity_id=file.id, action=ActionType.CREATED,
                               snapshot=self._snapshot_without_data(file))
        if plugin and plugin.has_after("create"):
            warnings += await plugin.after_save(FileSavedRequest(
                operation="create", user_id=user_id, previous=None,
                saved=self._metadata(file), changed_fields=tuple(FileCreate.model_fields),
            ))
        return FileWriteResult(file=file, plugin_warnings=warnings)

    async def get_file(
        self,
        user_id: UUID,
        file_id: int,
    ) -> File:
        """Get file by ID with ownership verification

        Args:
            user_id: User ID for ownership verification
            file_id: File ID to retrieve

        Returns:
            File with full details including base64 data

        Raises:
            NotFoundError: If file not found or not owned by user
        """
        logger.info(
            "getting file",
            extra={
                "file_id": file_id,
                "user_id": str(user_id),
            },
        )

        file = await self.file_repo.get_file_by_id(
            user_id=user_id,
            file_id=file_id,
        )

        if not file:
            raise NotFoundError(f"File {file_id} not found")

        logger.info(
            "file retrieved",
            extra={
                "file_id": file_id,
                "user_id": str(user_id),
            },
        )

        if settings.ACTIVITY_TRACK_READS and self._event_bus:
            await self._emit_event(
                user_id=user_id,
                entity_type=EntityType.FILE,
                entity_id=file_id,
                action=ActionType.READ,
                snapshot=self._snapshot_without_data(file),
            )

        return file

    async def list_files(
        self,
        user_id: UUID,
        project_id: int | None = None,
        mime_type: str | None = None,
        tags: list[str] | None = None,
    ) -> list[FileSummary]:
        """List files with optional filtering

        Args:
            user_id: User ID for ownership filtering
            project_id: Optional filter by project
            mime_type: Optional filter by MIME type
            tags: Optional filter by tags (returns files with ANY of these tags)

        Returns:
            List of FileSummary (lightweight, excludes base64 data)
        """
        logger.info(
            "listing files",
            extra={
                "user_id": str(user_id),
                "project_id": project_id,
                "mime_type": mime_type,
                "tags": tags,
            },
        )

        files = await self.file_repo.list_files(
            user_id=user_id,
            project_id=project_id,
            mime_type=mime_type,
            tags=tags,
        )

        logger.info(
            "files retrieved",
            extra={
                "count": len(files),
                "user_id": str(user_id),
            },
        )

        if settings.ACTIVITY_TRACK_READS and self._event_bus:
            await self._emit_event(
                user_id=user_id,
                entity_type=EntityType.FILE,
                entity_id=0,
                action=ActionType.QUERIED,
                snapshot={
                    "result_ids": [f.id for f in files],
                    "total_count": len(files),
                },
                metadata={
                    "project_id": project_id,
                    "mime_type": mime_type,
                    "tags": tags,
                },
            )

        return files

    async def _update_state(self, user_id, file_id, core, content, watching):
        patch = core.model_dump(exclude_unset=True)
        full = None
        if not watching or patch.get("data") is not None:
            # Preserve the old full read without registration; policy adds no blob read.
            full = await self.file_repo.get_file_by_id(user_id=user_id, file_id=file_id)
            previous = None if full is None else self._metadata(full)
        else:
            previous = await self.file_repo.get_file_metadata_by_id(user_id=user_id, file_id=file_id)
        if previous is None:
            raise NotFoundError(f"File {file_id} not found")
        previous = previous.model_copy(deep=True)
        old = previous.model_dump()
        changed = [name for name, value in patch.items()
                   if name != "data" and old.get(name) != value]
        if "data" in patch:
            if patch["data"] is None:
                changed.append("data")
            elif (content != base64.b64decode(full.data) if watching else patch["data"] != full.data):
                changed.append("data")
        # Preserve the existing event convention: data is never an event delta.
        event_changes = get_changed_fields(core, previous)
        proposed = old | {name: value for name, value in patch.items() if name != "data"}
        if patch.get("data") is not None and watching:
            proposed["size_bytes"] = len(content)
        return previous, tuple(changed), event_changes, proposed

    async def update_file(self, user_id: UUID, file_id: int, file_data: FileUpdate) -> FileWriteResult:
        """Update existing file (PATCH semantics)

        Only provided fields are updated. None/omitted fields remain unchanged.

        Args:
            user_id: User ID for ownership verification
            file_id: File ID to update
            file_data: FileUpdate with fields to change

        Returns:
            Updated File

        Raises:
            NotFoundError: If file not found or not owned by user
        """
        logger.info(
            "updating file",
            extra={
                "file_id": file_id,
                "user_id": str(user_id),
            },
        )
        submitted = file_data.model_copy(deep=True)
        core = apply_provenance_defaults_for_update(file_data.model_copy(deep=True))
        plugin = self._file_plugin
        watching = bool(plugin and (plugin.has_before("update") or plugin.has_after("update")))
        patch = core.model_dump(exclude_unset=True)
        content = (base64.b64decode(patch["data"])
                   if watching and patch.get("data") is not None else None)
        previous, changed, event_changes, proposed = await self._update_state(
            user_id, file_id, core, content, watching,
        )
        warnings = []
        if changed and plugin and plugin.has_before("update"):
            await self._check_project(user_id, patch.get("project_id"))
            warnings = await plugin.before_save(FileWriteRequest(
                operation="update", user_id=user_id,
                submitted=self._submission(submitted, file_id), previous=previous,
                proposed=FilePendingMetadata.model_validate(proposed), content=content,
                content_availability="submitted" if "data" in patch else "not_submitted",
                changed_fields=changed,
            ))
            previous, changed, event_changes, proposed = await self._update_state(
                user_id, file_id, core, content, watching,
            )
            await self._check_project(user_id, patch.get("project_id"))
        # Retain the original update/timestamp even for an effective no-op.
        file = await self.file_repo.update_file(user_id=user_id, file_id=file_id, file_data=core)
        logger.info(
            "file updated",
            extra={
                "file_id": file_id,
                "user_id": str(user_id),
            },
        )
        if event_changes:
            changes_dict = {field: {"old": old, "new": new}
                            for field, (old, new) in event_changes.items() if field != "data"}
            if changes_dict:
                await self._emit_event(user_id=user_id, entity_type=EntityType.FILE,
                                       entity_id=file_id, action=ActionType.UPDATED,
                                       snapshot=self._snapshot_without_data(file), changes=changes_dict)
        if changed and plugin and plugin.has_after("update"):
            warnings += await plugin.after_save(FileSavedRequest(
                operation="update", user_id=user_id, previous=previous,
                saved=self._metadata(file), changed_fields=changed,
            ))
        return FileWriteResult(file=file, plugin_warnings=warnings)

    async def delete_file(self, user_id: UUID, file_id: int) -> FileDeleteResult:
        """Delete file (cascade removes memory/entity associations)

        Args:
            user_id: User ID for ownership verification
            file_id: File ID to delete

        Returns:
            True if deleted, False if not found or not owned by user
        """
        logger.info(
            "deleting file",
            extra={
                "file_id": file_id,
                "user_id": str(user_id),
            },
        )
        warnings = []
        plugin = self._file_plugin
        watching = bool(plugin and (plugin.has_before("delete") or plugin.has_after("delete")))
        if watching:
            previous = await self.file_repo.get_file_metadata_by_id(user_id=user_id, file_id=file_id)
        else:
            full = await self.file_repo.get_file_by_id(user_id=user_id, file_id=file_id)
            previous = None if full is None else self._metadata(full)
        if previous is None:
            if not watching:
                deleted = await self.file_repo.delete_file(user_id=user_id, file_id=file_id)
                if deleted:
                    logger.info(
                        "file deleted",
                        extra={
                            "file_id": file_id,
                            "user_id": str(user_id),
                        },
                    )
                else:
                    logger.warning(
                        "file not found for deletion",
                        extra={
                            "file_id": file_id,
                            "user_id": str(user_id),
                        },
                    )
                return FileDeleteResult(deleted=deleted)
            logger.warning(
                "file not found for deletion",
                extra={
                    "file_id": file_id,
                    "user_id": str(user_id),
                },
            )
            return FileDeleteResult(deleted=False)
        previous = previous.model_copy(deep=True)
        if plugin and plugin.has_before("delete"):
            warnings = await plugin.before_save(FileWriteRequest(
                operation="delete", user_id=user_id,
                submitted=FileSubmission(metadata=FilePendingMetadata(), file_id=file_id),
                previous=previous, proposed=None,
            ))
            previous = await self.file_repo.get_file_metadata_by_id(user_id=user_id, file_id=file_id)
            if previous is None:
                logger.warning(
                    "file not found for deletion",
                    extra={
                        "file_id": file_id,
                        "user_id": str(user_id),
                    },
                )
                return FileDeleteResult(deleted=False, plugin_warnings=warnings)
            previous = previous.model_copy(deep=True)
        success = await self.file_repo.delete_file(user_id=user_id, file_id=file_id)
        if success:
            logger.info(
                "file deleted",
                extra={
                    "file_id": file_id,
                    "user_id": str(user_id),
                },
            )
            await self._emit_event(user_id=user_id, entity_type=EntityType.FILE,
                                   entity_id=file_id, action=ActionType.DELETED,
                                   snapshot=self._snapshot_without_data(previous))
            if plugin and plugin.has_after("delete"):
                warnings += await plugin.after_save(FileDeletedRequest(
                    user_id=user_id, deleted_file_id=file_id, previous=previous,
                ))
        else:
            logger.warning(
                "file not found for deletion",
                extra={
                    "file_id": file_id,
                    "user_id": str(user_id),
                },
            )
        return FileDeleteResult(deleted=success, plugin_warnings=warnings)
