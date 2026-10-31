"""Skill policy registration and production startup on isolated SQLite."""

import importlib
import sys

import pytest
from pydantic import ValidationError

from app.bootstrap import build_runtime, dispose_runtime
from app.config.settings import Settings, settings
from app.config.skill_plugin import load_skill_plugin
from app.models.write_plugin_models import WriteDecision
from app.protocols.skill_plugin_protocol import SkillPlugin


@pytest.fixture
def skill_plugin_runtime(sqlite_runtime_settings, monkeypatch, tmp_path):
    from test_skill_write_plugin import RecordingEmbeddings

    monkeypatch.setattr("app.bootstrap.get_embedding_adapter", RecordingEmbeddings)
    monkeypatch.setattr("app.bootstrap.get_reranker_adapter", lambda: None)
    monkeypatch.setattr("app.bootstrap.check_first_run_models", lambda: None)
    monkeypatch.syspath_prepend(str(tmp_path))
    module = "synthetic_skill_policy"
    (tmp_path / f"{module}.py").write_text("""
from app.models.write_plugin_models import WriteDecision, PluginWarning
from app.protocols.skill_plugin_protocol import SkillPlugin
calls = []
async def before(request):
    calls.append(("before", request.operation))
    state = request.previous if request.operation == "delete" else request.proposed
    if "blocked" in state.tags:
        return WriteDecision(outcome="reject", message="Skill rejected.")
    if "error" in state.tags:
        raise RuntimeError("synthetic-secret")
    if "warn" in state.tags:
        return WriteDecision(outcome="warn", message="Review instructions.")
    return WriteDecision(outcome="allow")
async def after(request):
    calls.append(("after", request.operation))
    state = request.previous if request.operation == "delete" else request.saved
    if "post-error" in state.tags:
        raise RuntimeError("synthetic-secret")
    if "post-warn" in state.tags:
        return PluginWarning(message="Review saved skill.")
plugin = SkillPlugin(
    before_save={op: before for op in ("create", "update", "delete")},
    after_save={op: after for op in ("create", "update", "delete")},
)
""")
    monkeypatch.setattr(settings, "SKILL_WRITE_PLUGIN", module + ":plugin")
    monkeypatch.setattr(settings, "SKILL_WRITE_PLUGIN_MODE", "enforce")
    monkeypatch.setattr(settings, "SKILL_WRITE_PLUGIN_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_WRITE_PLUGIN", "")
    try:
        yield module
    finally:
        sys.modules.pop(module, None)


@pytest.mark.parametrize("phase", ["before_save", "after_save"])
def test_registration_rejects_unsupported_or_sync_handlers(phase):
    async def handler(request):
        return WriteDecision(outcome="allow")

    with pytest.raises(ValueError, match="unsupported skill operation"):
        SkillPlugin(**{phase: {"link": handler}})
    with pytest.raises(ValueError, match="async functions"):
        SkillPlugin(**{phase: {"create": lambda request: None}})


def test_registration_copies_and_freezes_explicit_operations():
    async def handler(request):
        return WriteDecision(outcome="allow")

    handlers = {"create": handler}
    plugin = SkillPlugin(before_save=handlers)
    handlers["delete"] = handler
    assert set(plugin.before_save) == {"create"}
    with pytest.raises(TypeError):
        plugin.before_save["delete"] = handler


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_timeout_must_be_positive_and_finite(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SKILL_WRITE_PLUGIN_TIMEOUT_SECONDS=value)


@pytest.mark.parametrize("enabled", [False, True])
async def test_bad_registration_fails_before_provider_or_database_startup(
    skill_plugin_runtime, monkeypatch, enabled,
):
    monkeypatch.setattr(settings, "SKILLS_ENABLED", enabled)
    monkeypatch.setattr(settings, "SKILL_WRITE_PLUGIN", "invalid:missing")

    def forbidden():
        pytest.fail("Invalid registration must fail before resource initialization")

    monkeypatch.setattr("app.bootstrap.check_first_run_models", forbidden)
    with pytest.raises(ValueError, match="SKILL_WRITE_PLUGIN import failed"):
        await build_runtime()


async def test_valid_registration_is_loaded_but_inactive_when_skills_disabled(
    skill_plugin_runtime, monkeypatch,
):
    monkeypatch.setattr(settings, "SKILLS_ENABLED", False)
    runtime = await build_runtime()
    try:
        assert runtime.services.skill is None
        assert importlib.import_module(skill_plugin_runtime).calls == []
    finally:
        await dispose_runtime(runtime)


def test_empty_reference_disables_only_skill_plugin():
    config = Settings(_env_file=None, SKILL_WRITE_PLUGIN="")
    assert load_skill_plugin(config) is None
