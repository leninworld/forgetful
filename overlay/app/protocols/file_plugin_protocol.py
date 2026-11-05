"""A trusted File registration with explicit per-operation phase maps."""
import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, get_args

from app.models.file_plugin_models import (
    FileDeletedRequest, FileOperation, FileSavedRequest, FileWriteRequest,
)
from app.models.write_plugin_models import PluginWarning, WriteDecision


class FileBeforeSave(Protocol):
    async def __call__(self, request: FileWriteRequest) -> WriteDecision:
        ...


class FileAfterSave(Protocol):
    async def __call__(self, request: FileSavedRequest | FileDeletedRequest) -> PluginWarning | None:
        ...


@dataclass(frozen=True)
class FilePlugin:
    before_save: Mapping[FileOperation, FileBeforeSave] = field(default_factory=dict)
    after_save: Mapping[FileOperation, FileAfterSave] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for phase in ("before_save", "after_save"):
            handlers = getattr(self, phase)
            if not isinstance(handlers, Mapping):
                raise ValueError(f"{phase} must be an operation-to-handler mapping")
            handlers = dict(handlers)
            for operation, handler in handlers.items():
                if operation not in get_args(FileOperation):
                    raise ValueError(f"{phase} contains an unsupported file operation")
                if not inspect.iscoroutinefunction(handler):
                    raise ValueError(f"{phase} handlers must be async functions")
                try:
                    inspect.signature(handler).bind(object())
                except (TypeError, ValueError):
                    raise ValueError(f"{phase} handlers must accept one context argument") from None
            object.__setattr__(self, phase, MappingProxyType(handlers))
