"""Frozen operation maps for one trusted administrator Task Plugin."""

import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, get_args

from app.models.task_plugin_models import TaskAfterRequest, TaskBeforeRequest, TaskOperation
from app.models.write_plugin_models import PluginWarning, WriteDecision


class TaskBeforeSave(Protocol):
    async def __call__(self, request: TaskBeforeRequest) -> WriteDecision:
        ...


class TaskAfterSave(Protocol):
    async def __call__(self, request: TaskAfterRequest) -> PluginWarning | None:
        ...


@dataclass(frozen=True)
class TaskPlugin:
    before_save: Mapping[TaskOperation, TaskBeforeSave] = field(default_factory=dict)
    after_save: Mapping[TaskOperation, TaskAfterSave] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for phase in ("before_save", "after_save"):
            handlers = getattr(self, phase)
            if not isinstance(handlers, Mapping):
                raise ValueError(f"{phase} must be an operation-to-handler mapping")
            handlers = dict(handlers)
            for operation, handler in handlers.items():
                if operation not in get_args(TaskOperation):
                    raise ValueError(f"{phase} contains an unsupported task operation")
                if not inspect.iscoroutinefunction(handler):
                    raise ValueError(f"{phase} handlers must be async functions")
                try:
                    inspect.signature(handler).bind(object())
                except (TypeError, ValueError):
                    raise ValueError(f"{phase} handlers must accept one context argument") from None
            object.__setattr__(self, phase, MappingProxyType(handlers))
