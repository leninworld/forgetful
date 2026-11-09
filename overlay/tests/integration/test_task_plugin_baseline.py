"""Baseline-compatible policy oracle: only persisted successes count as missing hooks."""

import os
import sys
from uuid import uuid4

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from plan_plugin_helpers import execute_tool, plan_app
from plan_plugin_helpers import plan_runtime as plan_runtime

from app.config.settings import settings

SOURCE = """
from app.models.write_plugin_models import WriteDecision
from app.protocols.task_plugin_protocol import TaskPlugin
async def before(request):
    if request.operation == "create" and request.submitted.title == "Policy blocked create":
        return WriteDecision(outcome="reject", message="Synthetic create rejected")
    if request.operation == "claim" and request.submitted.agent_id == "policy-blocked-agent":
        return WriteDecision(outcome="reject", message="Synthetic claim rejected")
    return WriteDecision(outcome="allow")
registration = TaskPlugin(before_save={"create": before, "claim": before})
"""


@pytest.fixture
def baseline_task_policy(plan_runtime, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PLAN_WRITE_PLUGIN", "")
    name = f"synthetic_task_baseline_{uuid4().hex}"
    (tmp_path / f"{name}.py").write_text(SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    reference = f"{name}:registration"
    monkeypatch.setenv("TASK_WRITE_PLUGIN", reference)
    # Old Settings ignores this unknown environment key; no product import shim.
    if hasattr(settings, "TASK_WRITE_PLUGIN"):
        monkeypatch.setattr(settings, "TASK_WRITE_PLUGIN", reference)
    try:
        yield reference
    finally:
        sys.modules.pop(name, None)


async def seed_plan(client):
    project = await execute_tool(client, "create_project", {
        "name": "Task oracle", "description": "Synthetic persisted oracle", "project_type": "development",
    })
    return await execute_tool(client, "create_plan", {
        "title": "Oracle plan", "project_id": project["id"], "status": "active",
    })


async def test_task_create_policy_contract(baseline_task_policy):
    assert os.environ["TASK_WRITE_PLUGIN"] == baseline_task_policy
    async with Client(plan_app()) as client:
        plan = await seed_plan(client)
        try:
            created = await execute_tool(client, "create_task", {
                "plan_id": plan["id"], "title": "Policy blocked create",
            })
        except ToolError as exc:
            assert "TASK_PLUGIN_REJECTED" in str(exc), "wrong_task_policy_failure_reason"
            rows = await execute_tool(client, "query_tasks", {"plan_id": plan["id"]})
            assert rows["tasks"] == [], "rejected_create_persisted"
            return
        stored = await execute_tool(client, "get_task", {"task_id": created["id"]})
        assert stored["title"] == "Policy blocked create", "baseline_persistence_not_proven"
        assert stored["plan_id"] == plan["id"], "baseline_persistence_not_proven"
        raise AssertionError("missing_task_plugin_create_policy")


async def test_task_claim_policy_contract(baseline_task_policy):
    assert os.environ["TASK_WRITE_PLUGIN"] == baseline_task_policy
    async with Client(plan_app()) as client:
        plan = await seed_plan(client)
        task = await execute_tool(client, "create_task", {"plan_id": plan["id"], "title": "Allowed seed"})
        try:
            await execute_tool(client, "claim_task", {
                "task_id": task["id"], "agent_id": "policy-blocked-agent", "version": task["version"],
            })
        except ToolError as exc:
            assert "TASK_PLUGIN_REJECTED" in str(exc), "wrong_task_policy_failure_reason"
            stored = await execute_tool(client, "get_task", {"task_id": task["id"]})
            assert stored["state"] == "todo" and stored["version"] == task["version"], "rejected_claim_persisted"
            return
        stored = await execute_tool(client, "get_task", {"task_id": task["id"]})
        assert stored["state"] == "doing" and stored["assigned_agent"] == "policy-blocked-agent", "baseline_persistence_not_proven"
        assert stored["version"] == task["version"] + 1, "baseline_persistence_not_proven"
        raise AssertionError("missing_task_plugin_claim_policy")


async def test_no_plugin_legacy_task_control(baseline_task_policy, monkeypatch):
    monkeypatch.setenv("TASK_WRITE_PLUGIN", "")
    if hasattr(settings, "TASK_WRITE_PLUGIN"):
        monkeypatch.setattr(settings, "TASK_WRITE_PLUGIN", "")
    async with Client(plan_app()) as client:
        plan = await seed_plan(client)
        created = await execute_tool(client, "create_task", {"plan_id": plan["id"], "title": "Legacy task"})
        stored = await execute_tool(client, "get_task", {"task_id": created["id"]})
        assert created == stored
        assert stored["state"] == "todo" and stored["version"] == 1
        assert "plugin_warnings" not in stored
