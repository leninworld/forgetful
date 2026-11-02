"""Actual REST and MCP paths over persistent SQLite and production bootstrap."""

import importlib

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from httpx import ASGITransport, AsyncClient
from plan_plugin_helpers import execute_tool, plan_app
from plan_plugin_helpers import plan_runtime as plan_runtime

from app.bootstrap import build_runtime, dispose_runtime
from app.config.settings import settings


@pytest.fixture
async def policy_app(plan_runtime):
    app = plan_app()
    async with Client(app) as client:
        async with AsyncClient(
            transport=ASGITransport(app=app.http_app(), raise_app_exceptions=False), base_url="http://test",
        ) as http:
            yield app, client, http


async def project(client):
    return await execute_tool(client, "create_project", {
        "name": "Synthetic plan project", "description": "Transport contracts", "project_type": "development",
    })


async def test_rest_flat_warnings_errors_and_delete(policy_app):
    _, client, http = policy_app
    pid = (await project(client))["id"]
    for marker, status in [("plain", 201), ("warn", 201), ("blocked", 400), ("error", 503)]:
        response = await http.post("/api/v1/plans", json={"project_id": pid, "title": marker})
        assert response.status_code == status
        body = response.json()
        if status == 201:
            assert "plan" not in body and body["title"] == marker
            assert ("plugin_warnings" in body) == (marker == "warn")
        else:
            assert "SYNTHETIC_PRIVATE_ERROR" not in str(body)
    created = await http.post("/api/v1/plans", json={"project_id": pid, "title": "plain"})
    path = f"/api/v1/plans/{created.json()['id']}"
    assert (await http.put(path, json={"title": "blocked"})).status_code == 400
    assert (await http.put(path, json={"title": "error"})).status_code == 503
    warned = await http.put(path, json={"title": "post_warn"})
    assert warned.status_code == 200 and len(warned.json()["plugin_warnings"]) == 2
    assert "plugin_warnings" not in (await http.get(path)).json()
    deleted = await http.delete(path)
    assert deleted.status_code == 200 and deleted.json()["success"] is True
    assert len(deleted.json()["plugin_warnings"]) == 2
    assert (await http.delete(path)).status_code == 404


@pytest.mark.parametrize("marker,status", [("blocked", 400), ("error", 503)])
async def test_rest_delete_maps_policy_errors(policy_app, marker, status):
    app, client, http = policy_app
    pid = (await project(client))["id"]
    created = await http.post("/api/v1/plans", json={"project_id": pid, "title": "plain"})
    plan_id = created.json()["id"]
    # Seed the marker through the owned repository to target delete, not create policy.
    from app.models.plan_models import PlanUpdate
    # User is the runtime default created through the live transport.
    from app.models.user_models import UserCreate
    user = await app.user_service.get_or_create_user(UserCreate(
        external_id=settings.DEFAULT_USER_ID, name=settings.DEFAULT_USER_NAME, email=settings.DEFAULT_USER_EMAIL,
    ))
    await app.plan_service.plan_repo.update_plan(user.id, plan_id, PlanUpdate(title=marker))
    response = await http.delete(f"/api/v1/plans/{plan_id}")
    assert response.status_code == status
    assert (await http.get(f"/api/v1/plans/{plan_id}")).status_code == 200


async def test_mcp_warning_errors_reads_and_no_new_delete_tools(policy_app):
    app, client, _ = policy_app
    pid = (await project(client))["id"]
    created = await execute_tool(client, "create_plan", {"project_id": pid, "title": "warn"})
    assert created["plugin_warnings"][0]["code"] == "PLAN_POLICY" and "plan" not in created
    with pytest.raises(ToolError, match="PLAN_PLUGIN_REJECTED: Synthetic plan rejected"):
        await execute_tool(client, "create_plan", {"project_id": pid, "title": "blocked"})
    with pytest.raises(ToolError, match="PLAN_PLUGIN_UNAVAILABLE: Plan Write Plugin unavailable"):
        await execute_tool(client, "update_plan", {"plan_id": created["id"], "title": "error"})
    changed = await execute_tool(client, "update_plan", {"plan_id": created["id"], "title": "plain"})
    assert changed["id"] == created["id"] and "plugin_warnings" not in changed
    assert "plugin_warnings" not in await execute_tool(client, "get_plan", {"plan_id": created["id"]})
    listing = await execute_tool(client, "list_plans", {})
    assert all("plugin_warnings" not in p for p in listing["plans"])
    discovered = await client.call_tool("discover_forgetful_tools", {})
    assert "delete_plan" not in str(discovered.data) and "delete_task" not in str(discovered.data)


async def test_scope_and_auth_prevent_dispatch(policy_app, plan_runtime, monkeypatch):
    app, client, http = policy_app
    from app.routes.mcp.scope_resolver import parse_scopes, resolve_permitted_tools
    scope = parse_scopes("read")
    monkeypatch.setattr(app, "_instance_scopes", scope)
    monkeypatch.setattr(app, "_instance_permitted_tools", resolve_permitted_tools(scope, app.registry))
    with pytest.raises(ToolError, match="not permitted under current scopes"):
        await execute_tool(client, "create_plan", {"project_id": 1, "title": "blocked"})
    monkeypatch.setattr(app, "auth", object())
    assert (await http.post("/api/v1/plans", json={"project_id": 1, "title": "blocked"})).status_code == 401
    assert importlib.import_module(plan_runtime).before_calls == []


async def test_registration_validated_when_planning_disabled(plan_runtime, monkeypatch):
    monkeypatch.setattr(settings, "PLANNING_ENABLED", False)
    runtime = await build_runtime()
    try:
        assert runtime.services.plan is None and runtime.services.task is None
        module = importlib.import_module(plan_runtime)
        assert module.loads == 1 and module.before_calls == module.after_calls == []
    finally:
        await dispose_runtime(runtime)
    monkeypatch.setattr(settings, "PLAN_WRITE_PLUGIN", "invalid:missing")
    with pytest.raises(ValueError, match="PLAN_WRITE_PLUGIN"):
        await build_runtime()


@pytest.mark.parametrize("path", ["mcp_transition", "rest_transition", "rest_delete"])
async def test_task_success_exposes_secondary_rejection_on_wire(policy_app, path):
    app, client, http = policy_app
    pid = (await project(client))["id"]
    plan = await execute_tool(client, "create_plan", {"project_id": pid, "title": "plain", "status": "active"})
    target = await execute_tool(client, "create_task", {"plan_id": plan["id"], "title": "Target"})
    blocker = await execute_tool(client, "create_task", {"plan_id": plan["id"], "title": "Blocker"})
    doing = await execute_tool(client, "transition_task", {"task_id": target["id"], "state": "doing", "version": target["version"]})
    if path == "rest_delete":
        done = await execute_tool(client, "transition_task", {"task_id": target["id"], "state": "done", "version": doing["version"]})
        assert done["state"] == "done"
    else:
        assert (await http.delete(f"/api/v1/tasks/{blocker['id']}")).status_code == 200
    # Configure the next automatic plan decision through persisted synthetic data.
    from app.models.plan_models import PlanUpdate
    from app.models.user_models import UserCreate
    user = await app.user_service.get_or_create_user(UserCreate(
        external_id=settings.DEFAULT_USER_ID, name=settings.DEFAULT_USER_NAME, email=settings.DEFAULT_USER_EMAIL,
    ))
    await app.plan_service.plan_repo.update_plan(user.id, plan["id"], PlanUpdate(title="blocked"))
    if path == "mcp_transition":
        payload = await execute_tool(client, "transition_task", {"task_id": target["id"], "state": "done", "version": doing["version"]})
    elif path == "rest_transition":
        response = await http.post(f"/api/v1/tasks/{target['id']}/transition", json={"state": "done", "version": doing["version"]})
        assert response.status_code == 200
        payload = response.json()
    else:
        response = await http.delete(f"/api/v1/tasks/{blocker['id']}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"]
    assert payload["plugin_warnings"][0]["code"] == "plan_auto_completion_rejected"
    if path != "rest_delete":
        assert payload["state"] == "done" and payload["version"] == doing["version"] + 1
    assert (await http.get(f"/api/v1/plans/{plan['id']}")).json()["status"] == "active"
