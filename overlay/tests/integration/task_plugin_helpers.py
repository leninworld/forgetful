"""Synthetic Task registration; reuse the proven real Plan transport fixture."""

import sys
from uuid import uuid4

import pytest
from plan_plugin_helpers import plan_runtime as plan_runtime

from app.config.settings import settings

REGISTRATION_SOURCE = """
from app.models.write_plugin_models import PluginWarning, WriteDecision
from app.protocols.task_plugin_protocol import TaskPlugin
outcome = "allow"
after_warning = False
after_error = False
before_calls = []
after_calls = []
loads = 0
async def before(request):
    before_calls.append(request.model_copy(deep=True))
    if outcome == "error":
        raise RuntimeError("SYNTHETIC_PRIVATE_TASK_ERROR")
    return WriteDecision(outcome=outcome, message="Synthetic task decision", code="TASK_POLICY")
async def after(request):
    after_calls.append(request.model_copy(deep=True))
    if after_error:
        raise RuntimeError("SYNTHETIC_PRIVATE_TASK_ERROR")
    if after_warning:
        return PluginWarning(message="Synthetic task after", code="TASK_AFTER")
def __getattr__(name):
    global loads
    if name != "registration":
        raise AttributeError(name)
    loads += 1
    operations = ("create", "update", "delete", "claim", "transition", "criterion_add",
                  "criterion_update", "criterion_delete", "dependency_add", "dependency_remove")
    return TaskPlugin(before_save=dict.fromkeys(operations, before), after_save=dict.fromkeys(operations, after))
"""


@pytest.fixture
def task_runtime(plan_runtime, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PLAN_WRITE_PLUGIN", "")
    monkeypatch.setattr(settings, "ENTITY_WRITE_PLUGIN", "")
    monkeypatch.setattr(settings, "TASK_WRITE_PLUGIN_MODE", "enforce")
    monkeypatch.setattr(settings, "TASK_WRITE_PLUGIN_TIMEOUT_SECONDS", 5.0)
    name = f"synthetic_task_policy_{uuid4().hex}"
    (tmp_path / f"{name}.py").write_text(REGISTRATION_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(settings, "TASK_WRITE_PLUGIN", f"{name}:registration")
    try:
        yield name
    finally:
        sys.modules.pop(name, None)


def operation_call(operation, seed):
    """Only existing real operations; no task-delete MCP endpoint is invented."""
    task, peer, criterion, plan = seed["task"], seed["peer"], seed["criterion"], seed["plan"]
    tid = task["id"]
    rows = {
        "create": ("create_task", {"plan_id": plan["id"], "title": "New task"}, "POST", "/api/v1/tasks"),
        "update": ("update_task", {"task_id": tid, "title": "Changed task"}, "PUT", f"/api/v1/tasks/{tid}"),
        "delete": (None, {}, "DELETE", f"/api/v1/tasks/{tid}"),
        "claim": ("claim_task", {"task_id": tid, "agent_id": "synthetic-agent", "version": task["version"]}, "POST", f"/api/v1/tasks/{tid}/claim"),
        "transition": ("transition_task", {"task_id": tid, "state": "doing", "version": task["version"]}, "POST", f"/api/v1/tasks/{tid}/transition"),
        "criterion_add": ("add_criterion", {"task_id": tid, "description": "New criterion"}, "POST", f"/api/v1/tasks/{tid}/criteria"),
        "criterion_update": ("verify_criterion", {"criterion_id": criterion["id"], "met": True}, "PUT", f"/api/v1/criteria/{criterion['id']}"),
        "criterion_delete": ("delete_criterion", {"criterion_id": criterion["id"]}, "DELETE", f"/api/v1/criteria/{criterion['id']}"),
        "dependency_add": ("add_dependency", {"task_id": tid, "depends_on_task_id": peer["id"]}, "POST", f"/api/v1/tasks/{tid}/dependencies"),
        "dependency_remove": ("remove_dependency", {"task_id": tid, "depends_on_task_id": peer["id"]}, "DELETE", f"/api/v1/tasks/{tid}/dependencies/{peer['id']}"),
    }
    return rows[operation]


async def seed_mcp(call, operation):
    project = await call("create_project", {"name": "Task transport", "description": "Synthetic", "project_type": "development"})
    plan = await call("create_plan", {"title": "Task plan", "project_id": project["id"], "status": "active"})
    task = await call("create_task", {"title": "Target", "plan_id": plan["id"]})
    peer = await call("create_task", {"title": "Peer", "plan_id": plan["id"]})
    doing = await call("transition_task", {"task_id": peer["id"], "state": "doing", "version": peer["version"]})
    peer = await call("transition_task", {"task_id": peer["id"], "state": "done", "version": doing["version"]})
    criterion = await call("add_criterion", {"task_id": task["id"], "description": "Stored criterion"})
    if operation != "dependency_add":
        await call("add_dependency", {"task_id": task["id"], "depends_on_task_id": peer["id"]})
    return {"task": task, "peer": peer, "criterion": criterion, "plan": plan}


def seed_sync(call, operation):
    project = call("create_project", {"name": "Task CLI", "description": "Synthetic", "project_type": "development"})
    plan = call("create_plan", {"title": "Task plan", "project_id": project["id"], "status": "active"})
    task = call("create_task", {"title": "Target", "plan_id": plan["id"]})
    peer = call("create_task", {"title": "Peer", "plan_id": plan["id"]})
    doing = call("transition_task", {"task_id": peer["id"], "state": "doing", "version": peer["version"]})
    peer = call("transition_task", {"task_id": peer["id"], "state": "done", "version": doing["version"]})
    criterion = call("add_criterion", {"task_id": task["id"], "description": "Stored criterion"})
    if operation != "dependency_add":
        call("add_dependency", {"task_id": task["id"], "depends_on_task_id": peer["id"]})
    return {"task": task, "peer": peer, "criterion": criterion, "plan": plan}
