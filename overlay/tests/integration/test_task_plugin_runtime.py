"""Task registration, deadlines, isolated snapshots and cancellation boundaries."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config.settings import Settings
from app.config.task_plugin import load_task_plugin
from app.exceptions import TaskPluginRejectedError, TaskPluginUnavailableError
from app.models.plan_models import Plan, Task, TaskCreate
from app.models.task_plugin_models import TaskCreateRequest, TaskSavedRequest
from app.models.write_plugin_models import WriteDecision
from app.protocols.task_plugin_protocol import TaskPlugin
from app.services.task_write_plugin_service import TaskWritePluginService


def before_request():
    now = datetime.now(UTC)
    plan = Plan(id=1, project_id=1, title="Synthetic plan", created_at=now, updated_at=now)
    data = TaskCreate(plan_id=1, title="Submitted task", source_files=["synthetic.py"])
    return TaskCreateRequest(user_id=uuid4(), plan=plan, submitted=data, proposed=data.model_copy(deep=True))


def after_request():
    before = before_request()
    now = datetime.now(UTC)
    saved = Task(id=1, plan_id=1, title="Saved task", created_at=now, updated_at=now)
    return TaskSavedRequest(operation="create", user_id=before.user_id, plan=before.plan, previous=None, saved=saved)


async def allow(request):
    return WriteDecision(outcome="allow")


@pytest.mark.parametrize("phase", ["before_save", "after_save"])
@pytest.mark.parametrize("handlers", [None, [], {"obsolete": allow}, {"create": lambda request: None}])
def test_invalid_registration_maps(phase, handlers):
    with pytest.raises(ValueError):
        TaskPlugin(**{phase: handlers})


def test_registration_copies_freezes_and_checks_signature():
    async def wrong(first, second):
        return WriteDecision(outcome="allow")

    with pytest.raises(ValueError, match="one context"):
        TaskPlugin(before_save={"create": wrong})
    mapping = {"create": allow}
    plugin = TaskPlugin(before_save=mapping)
    mapping.clear()
    assert plugin.before_save["create"] is allow
    with pytest.raises(TypeError):
        plugin.before_save["delete"] = allow


@pytest.mark.parametrize("reference", ["bad", ":handler", "bad-module:handler", "good:a.b"])
def test_loader_rejects_invalid_reference(reference):
    with pytest.raises(ValueError, match="module:object"):
        load_task_plugin(SimpleNamespace(TASK_WRITE_PLUGIN=reference))


def test_loader_disabled_and_rejects_other_registration(monkeypatch):
    assert load_task_plugin(SimpleNamespace(TASK_WRITE_PLUGIN=" ")) is None
    monkeypatch.setattr("app.config.task_plugin.importlib.import_module", lambda name: SimpleNamespace(plugin=object()))
    with pytest.raises(ValueError, match="TaskPlugin registration"):
        load_task_plugin(SimpleNamespace(TASK_WRITE_PLUGIN="synthetic:plugin"))


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_timeout_setting_is_positive_and_finite(timeout):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, TASK_WRITE_PLUGIN_TIMEOUT_SECONDS=timeout)


@pytest.mark.asyncio
async def test_no_registration_and_handler_mutations_are_detached():
    before = before_request()
    after = after_request()
    assert await TaskWritePluginService(TaskPlugin()).before_save(before) == []
    assert await TaskWritePluginService(TaskPlugin()).after_save(after) == []

    async def change_before(request):
        request.submitted.title = "Not persisted"
        request.proposed.source_files.append("Not persisted nested")
        request.plan.project_id = -1
        return WriteDecision(outcome="allow")

    async def change_after(request):
        request.saved.title = "Not returned"

    runner = TaskWritePluginService(TaskPlugin(
        before_save={"create": change_before}, after_save={"create": change_after},
    ))
    assert await runner.before_save(before) == []
    assert await runner.after_save(after) == []
    assert before.submitted.title == "Submitted task"
    assert before.proposed.source_files == ["synthetic.py"]
    assert before.plan.project_id == 1 and after.saved.title == "Saved task"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["warn", "enforce"])
@pytest.mark.parametrize("outcome", ["allow", "warn", "reject"])
async def test_before_decision_modes(mode, outcome):
    async def handler(request):
        return WriteDecision(outcome=outcome, message="Synthetic decision", code="TASK_DECISION")

    runner = TaskWritePluginService(TaskPlugin(before_save={"create": handler}), mode=mode)
    if mode == "enforce" and outcome == "reject":
        with pytest.raises(TaskPluginRejectedError, match="Synthetic decision"):
            await runner.before_save(before_request())
    else:
        warnings = await runner.before_save(before_request())
        assert [w.code for w in warnings] == ([] if outcome == "allow" else ["TASK_DECISION"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["warn", "enforce"])
@pytest.mark.parametrize("failure", ["error", "malformed", "timeout", "child_cancel"])
async def test_before_unavailable_is_sanitized_and_child_cancel_is_not_request_cancel(mode, failure, caplog):
    async def handler(request):
        if failure == "error":
            raise RuntimeError("SYNTHETIC_PRIVATE_TASK_ERROR")
        if failure == "timeout":
            await asyncio.sleep(60)
        if failure == "child_cancel":
            child = asyncio.create_task(asyncio.sleep(60))
            child.cancel()
            await child
        return {"outcome": "warn", "message": "", "secret": "SYNTHETIC_PRIVATE_TASK_ERROR"}

    runner = TaskWritePluginService(TaskPlugin(before_save={"create": handler}), mode=mode, timeout_seconds=0.001)
    if mode == "enforce":
        with pytest.raises(TaskPluginUnavailableError, match="Task Write Plugin unavailable"):
            await runner.before_save(before_request())
    else:
        warnings = await runner.before_save(before_request())
        assert [w.code for w in warnings] == ["TASK_PLUGIN_UNAVAILABLE"]
        assert "SYNTHETIC_PRIVATE_TASK_ERROR" not in str(warnings)
    assert "SYNTHETIC_PRIVATE_TASK_ERROR" not in caplog.text
    assert asyncio.current_task().cancelling() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["warn", "enforce"])
@pytest.mark.parametrize("failure", ["error", "malformed", "timeout", "child_cancel"])
async def test_after_failures_never_veto_saved_result(mode, failure, caplog):
    async def handler(request):
        if failure == "error":
            raise RuntimeError("SYNTHETIC_PRIVATE_TASK_ERROR")
        if failure == "timeout":
            await asyncio.sleep(60)
        if failure == "child_cancel":
            child = asyncio.create_task(asyncio.sleep(60))
            child.cancel()
            await child
        return object()

    runner = TaskWritePluginService(TaskPlugin(after_save={"create": handler}), mode=mode, timeout_seconds=0.001)
    warnings = await runner.after_save(after_request())
    assert [w.code for w in warnings] == ["TASK_PLUGIN_AFTER_SAVE_UNAVAILABLE"]
    assert "SYNTHETIC_PRIVATE_TASK_ERROR" not in caplog.text
    assert asyncio.current_task().cancelling() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_save", "after_save"])
async def test_genuine_request_cancellation_propagates(phase):
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    runner = TaskWritePluginService(TaskPlugin(**{phase: {"create": handler}}))
    context = before_request() if phase == "before_save" else after_request()
    work = asyncio.create_task(getattr(runner, phase)(context))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
    finally:
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)
