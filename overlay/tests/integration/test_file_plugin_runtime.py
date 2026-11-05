"""Startup registration validation, explicit maps and disabled-feature behavior."""
import logging

import pytest
from file_plugin_helpers import file_runtime as file_runtime
from pydantic import ValidationError

from app.bootstrap import build_runtime, dispose_runtime
from app.config.settings import Settings, settings
from app.protocols.file_plugin_protocol import FilePlugin
from app.services.file_service import FileService
from app.services.file_write_plugin_service import FileWritePluginService


async def allow(ctx):
    return {"outcome": "allow"}


@pytest.mark.parametrize("handlers", [[], {"unknown": allow}, {"create": lambda x: None}])
def test_invalid_registration_rejected(handlers):
    with pytest.raises(ValueError):
        FilePlugin(before_save=handlers)


def test_registration_copies_maps_and_checks_actual_handlers():
    mappings = {"create": allow}
    plugin = FilePlugin(before_save=mappings)
    mappings.clear()
    assert "create" in plugin.before_save
    with pytest.raises(TypeError):
        plugin.before_save["update"] = allow
    class StaleItems(dict):
        def items(self):
            return {"create": allow}.items()
    with pytest.raises(ValueError):
        FilePlugin(before_save=StaleItems(create=lambda ctx: None))
    async def extra_required(first, second):
        return None
    with pytest.raises(ValueError, match="one context"):
        FilePlugin(after_save={"delete": extra_required})


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_timeout_is_positive_finite(timeout):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, FILE_WRITE_PLUGIN_TIMEOUT_SECONDS=timeout)


def test_runner_requires_project_dependency():
    with pytest.raises(ValueError, match="project repository"):
        FileService(None, file_plugin=FileWritePluginService(FilePlugin()))


@pytest.mark.parametrize("reference", ["missing_file_policy:plugin", "bad reference", ":plugin"])
@pytest.mark.parametrize("enabled", [True, False])
async def test_bad_registration_fails_before_provider(file_runtime, monkeypatch, reference, enabled):
    monkeypatch.setattr(settings, "FILE_WRITE_PLUGIN", reference)
    monkeypatch.setattr(settings, "FILES_ENABLED", enabled)
    def forbidden():
        raise AssertionError("Provider called before invalid registration was rejected")
    monkeypatch.setattr("app.bootstrap.get_embedding_adapter", forbidden)
    with pytest.raises(ValueError, match="FILE_WRITE_PLUGIN"):
        await build_runtime()


async def test_configured_plugin_validated_but_disabled_feature_has_no_dispatch(file_runtime, monkeypatch):
    import importlib
    monkeypatch.setattr(settings, "FILES_ENABLED", False)
    runtime = await build_runtime()
    try:
        registration = importlib.import_module(file_runtime)
        assert runtime.services.file is None
        assert registration.loads == 1
        assert registration.before_calls == [] and registration.after_calls == []
    finally:
        await dispose_runtime(runtime)


async def test_own_and_handler_module_loggers_reenabled_after_alembic(file_runtime):
    callback_logger = logging.getLogger(file_runtime)
    callback_logger.disabled = False
    runtime = await build_runtime()
    try:
        assert not callback_logger.disabled
        assert not logging.getLogger("app.services.file_write_plugin_service").disabled
    finally:
        await dispose_runtime(runtime)
