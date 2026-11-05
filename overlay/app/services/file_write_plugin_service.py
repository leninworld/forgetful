"""Run File callbacks outside transactions with cooperative deadlines."""
import asyncio
import logging
from typing import Literal

from app.exceptions import WritePluginRejectedError, WritePluginUnavailableError
from app.models.file_plugin_models import (
    FileDeletedRequest, FileOperation, FileSavedRequest, FileWriteRequest,
)
from app.models.write_plugin_models import PluginWarning, WriteDecision
from app.protocols.file_plugin_protocol import FilePlugin

logger = logging.getLogger(__name__)
UNAVAILABLE_MESSAGE = "File Write Plugin unavailable."


class FileWritePluginService:
    def __init__(self, plugin: FilePlugin, mode: Literal["warn", "enforce"] = "enforce",
                 timeout_seconds: float = 5.0):
        self._plugin = plugin
        self._mode = mode
        self._timeout_seconds = timeout_seconds

    def has_before(self, operation: FileOperation) -> bool:
        return operation in self._plugin.before_save

    def has_after(self, operation: FileOperation) -> bool:
        return operation in self._plugin.after_save

    def _before_unavailable(self, operation, exc):
        logger.warning("File Plugin unavailable (phase=before_save, operation=%s, mode=%s, error_type=%s)",
                       operation, self._mode, type(exc).__name__)
        if self._mode == "enforce":
            raise WritePluginUnavailableError(UNAVAILABLE_MESSAGE) from None
        return [PluginWarning(message=UNAVAILABLE_MESSAGE, code="WRITE_PLUGIN_UNAVAILABLE")]

    async def before_save(self, request: FileWriteRequest) -> list[PluginWarning]:
        operation = request.operation
        handler = self._plugin.before_save.get(operation)
        if handler is None:
            return []
        try:
            async with asyncio.timeout(self._timeout_seconds):
                decision = WriteDecision.model_validate(await handler(request.model_copy(deep=True)))
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            return self._before_unavailable(operation, exc)
        except Exception as exc:
            return self._before_unavailable(operation, exc)
        if decision.outcome == "reject":
            if self._mode == "enforce":
                raise WritePluginRejectedError(decision.message)
            decision = decision.model_copy(update={"outcome": "warn"})
        if decision.outcome == "warn":
            return [PluginWarning.model_validate(decision.model_dump())]
        return []

    async def after_save(self, request: FileSavedRequest | FileDeletedRequest) -> list[PluginWarning]:
        operation = request.operation
        handler = self._plugin.after_save.get(operation)
        if handler is None:
            return []
        try:
            async with asyncio.timeout(self._timeout_seconds):
                result = await handler(request.model_copy(deep=True))
                return [] if result is None else [PluginWarning.model_validate(result)]
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            return [self._after_unavailable(operation, exc)]
        except Exception as exc:
            return [self._after_unavailable(operation, exc)]

    @staticmethod
    def _after_unavailable(operation, exc) -> PluginWarning:
        logger.warning("File Plugin unavailable (phase=after_save, operation=%s, error_type=%s)",
                       operation, type(exc).__name__)
        outcome = "deleted" if operation == "delete" else "saved"
        return PluginWarning(message=f"File {outcome}, but after-save plugin unavailable.",
                             code="FILE_PLUGIN_AFTER_SAVE_UNAVAILABLE")
