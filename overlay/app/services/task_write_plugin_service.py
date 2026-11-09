"""Run registered Task Plugin policies with cooperative async deadlines."""

import asyncio
import logging
from typing import Literal

from app.exceptions import TaskPluginRejectedError, TaskPluginUnavailableError
from app.models.task_plugin_models import TaskAfterRequest, TaskBeforeRequest, TaskOperation
from app.models.write_plugin_models import PluginWarning, WriteDecision
from app.protocols.task_plugin_protocol import TaskPlugin

logger = logging.getLogger(__name__)
UNAVAILABLE_MESSAGE = "Task Write Plugin unavailable."


class TaskWritePluginService:
    def __init__(
        self, plugin: TaskPlugin, mode: Literal["warn", "enforce"] = "enforce",
        timeout_seconds: float = 5.0,
    ):
        self._plugin = plugin
        self._mode = mode
        self._timeout_seconds = timeout_seconds

    def has_before(self, operation: TaskOperation) -> bool:
        return operation in self._plugin.before_save

    def has_after(self, operation: TaskOperation) -> bool:
        return operation in self._plugin.after_save

    async def before_save(self, request: TaskBeforeRequest) -> list[PluginWarning]:
        handler = self._plugin.before_save.get(request.operation)
        if handler is None:
            return []
        try:
            async with asyncio.timeout(self._timeout_seconds):
                decision = WriteDecision.model_validate(await handler(request.model_copy(deep=True)))
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if task is None or task.cancelling():
                raise
            return self._before_unavailable(request.operation, exc)
        except Exception as exc:
            return self._before_unavailable(request.operation, exc)
        if decision.outcome == "reject":
            if self._mode == "enforce":
                raise TaskPluginRejectedError(decision.message)
            decision = decision.model_copy(update={"outcome": "warn"})
        if decision.outcome == "warn":
            return [PluginWarning.model_validate(decision.model_dump())]
        return []

    def _before_unavailable(
        self, operation: TaskOperation, exc: Exception | asyncio.CancelledError,
    ) -> list[PluginWarning]:
        logger.warning(
            "Task Plugin unavailable (phase=before_save, operation=%s, mode=%s, error_type=%s)",
            operation, self._mode, type(exc).__name__,
        )
        if self._mode == "enforce":
            raise TaskPluginUnavailableError(UNAVAILABLE_MESSAGE) from None
        return [PluginWarning(message=UNAVAILABLE_MESSAGE, code="TASK_PLUGIN_UNAVAILABLE")]

    async def after_save(self, request: TaskAfterRequest) -> list[PluginWarning]:
        handler = self._plugin.after_save.get(request.operation)
        if handler is None:
            return []
        try:
            async with asyncio.timeout(self._timeout_seconds):
                result = await handler(request.model_copy(deep=True))
                return [] if result is None else [PluginWarning.model_validate(result)]
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if task is None or task.cancelling():
                raise
            return [self.after_save_unavailable(request.operation, exc)]
        except Exception as exc:
            return [self.after_save_unavailable(request.operation, exc)]

    @staticmethod
    def after_save_unavailable(
        operation: TaskOperation, exc: Exception | asyncio.CancelledError,
    ) -> PluginWarning:
        logger.warning(
            "Task Plugin unavailable (phase=after_save, operation=%s, error_type=%s)",
            operation, type(exc).__name__,
        )
        return PluginWarning(
            message="Task mutation succeeded, but after-save plugin unavailable.",
            code="TASK_PLUGIN_AFTER_SAVE_UNAVAILABLE",
        )
