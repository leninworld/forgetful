"""Task policies through real MCP and HTTP routes with persistent SQLite."""

import asyncio
import importlib

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from httpx import ASGITransport, AsyncClient
from plan_plugin_helpers import execute_tool, plan_app
from task_plugin_helpers import operation_call, seed_mcp
from task_plugin_helpers import task_runtime as task_runtime

from app.bootstrap import build_runtime, dispose_runtime
from app.config.settings import settings

OPERATIONS = ("create", "update", "delete", "claim", "transition", "criterion_add", "criterion_update",
              "criterion_delete", "dependency_add", "dependency_remove")


@pytest.fixture
async def policy_app(task_runtime):
    app = plan_app()
    async with Client(app) as client:
        async with AsyncClient(transport=ASGITransport(app=app.http_app(), raise_app_exceptions=False), base_url="http://test") as http:
            yield app, client, http


async def seeded(client, operation):
    async def call(tool, arguments):
        async with asyncio.timeout(30):
            return await execute_tool(client, tool, arguments)
    return await seed_mcp(call, operation)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("outcome,status", [("reject", 400), ("error", 503)])
async def test_every_rest_mutation_maps_task_policy_errors(policy_app, task_runtime, operation, outcome, status):
    _, client, http = policy_app
    seed = await seeded(client, operation)
    module = importlib.import_module(task_runtime)
    module.before_calls.clear()
    module.after_calls.clear()
    module.outcome = outcome
    tool, arguments, method, path = operation_call(operation, seed)
    del tool
    body = {k: v for k, v in arguments.items() if k not in ("task_id", "criterion_id")}
    async with asyncio.timeout(30):
        response = await http.request(method, path, json=body if method != "DELETE" else None)
    assert response.status_code == status, response.text
    assert "SYNTHETIC_PRIVATE_TASK_ERROR" not in response.text
    assert [r.operation for r in module.before_calls] == [operation]
    assert module.after_calls == []
    assert (await http.get(f"/api/v1/tasks/{seed['task']['id']}")).json()["title"] == "Target"


@pytest.mark.parametrize("operation", [o for o in OPERATIONS if o != "delete"])
@pytest.mark.parametrize("outcome,code", [("reject", "TASK_PLUGIN_REJECTED"), ("error", "TASK_PLUGIN_UNAVAILABLE")])
async def test_every_supported_mcp_mutation_maps_task_errors(policy_app, task_runtime, operation, outcome, code):
    _, client, _ = policy_app
    seed = await seeded(client, operation)
    module = importlib.import_module(task_runtime)
    module.outcome = outcome
    module.before_calls.clear()
    module.after_calls.clear()
    tool, arguments, _, _ = operation_call(operation, seed)
    with pytest.raises(ToolError, match=code) as error:
        async with asyncio.timeout(30):
            await execute_tool(client, tool, arguments)
    assert "SYNTHETIC_PRIVATE_TASK_ERROR" not in str(error.value)
    assert [r.operation for r in module.before_calls] == [operation]
    assert module.after_calls == []


@pytest.mark.parametrize("operation", ["create", "criterion_add", "dependency_add", "criterion_delete"])
async def test_wire_warnings_are_flat_and_nonempty_only(policy_app, task_runtime, operation):
    _, client, _ = policy_app
    seed = await seeded(client, operation)
    module = importlib.import_module(task_runtime)
    module.outcome = "warn"
    module.after_warning = True
    tool, arguments, _, _ = operation_call(operation, seed)
    payload = await execute_tool(client, tool, arguments)
    assert [w["code"] for w in payload["plugin_warnings"]] == ["TASK_POLICY", "TASK_AFTER"]
    assert not any(key in payload for key in ("task", "criterion", "dependency"))
    if operation == "criterion_delete":
        assert payload["success"] is True
    else:
        assert payload["id"] > 0


async def test_task_no_warning_wire_bytes_equal_unchanged_read_model(policy_app):
    _, client, http = policy_app
    seed = await seeded(client, "create")
    response = await http.post("/api/v1/tasks", json={"plan_id": seed["plan"]["id"], "title": "Byte equality"})
    assert response.status_code == 201
    read = await http.get(f"/api/v1/tasks/{response.json()['id']}")
    assert response.content == read.content
    assert "plugin_warnings" not in response.json()
    assert (await http.delete("/api/v1/criteria/-1")).status_code == 404
    missing = await execute_tool(client, "delete_criterion", {"criterion_id": -1})
    assert missing == {"success": False}


async def test_disabled_planning_still_validates_task_registration(task_runtime, monkeypatch):
    monkeypatch.setattr(settings, "PLANNING_ENABLED", False)
    runtime = await build_runtime()
    try:
        assert runtime.services.task is None
        module = importlib.import_module(task_runtime)
        assert module.loads == 1 and module.before_calls == module.after_calls == []
    finally:
        await dispose_runtime(runtime)
    def forbidden_provider():
        pytest.fail("Invalid task registration must fail before provider initialization")

    monkeypatch.setattr("app.bootstrap.get_embedding_adapter", forbidden_provider)
    monkeypatch.setattr(settings, "TASK_WRITE_PLUGIN", "invalid:missing")
    with pytest.raises(ValueError, match="TASK_WRITE_PLUGIN"):
        await build_runtime()


async def test_memory_entity_plan_and_task_registrations_coexist(task_runtime, monkeypatch):
    from app.models.entity_models import EntityCreate, EntityType
    from app.models.memory_models import MemoryCreate
    from app.models.plan_models import PlanCreate, PlanStatus, TaskCreate
    from app.models.project_models import ProjectCreate
    from app.models.user_models import UserCreate
    from app.models.write_plugin_models import WriteDecision
    from app.protocols.plan_plugin_protocol import PlanPlugin
    from app.protocols.write_plugin_protocol import EntityPlugin, MemoryPlugin

    calls = []

    async def memory_before(request):
        calls.append("memory")
        return WriteDecision(outcome="allow")

    async def entity_before(request):
        calls.append("entity")
        return WriteDecision(outcome="allow")

    async def plan_before(request):
        calls.append("plan")
        return WriteDecision(outcome="allow")

    module = importlib.import_module(task_runtime)
    original_task_before = module.before

    async def task_before(request):
        calls.append("task")
        return await original_task_before(request)

    monkeypatch.setattr(module, "before", task_before)
    registrations = [
        ("MEMORY_WRITE_PLUGIN", "memory_registration", MemoryPlugin(before_save={"create": memory_before})),
        ("ENTITY_WRITE_PLUGIN", "entity_registration", EntityPlugin(before_save={"create": entity_before})),
        ("PLAN_WRITE_PLUGIN", "plan_registration", PlanPlugin(before_save={"create": plan_before})),
    ]
    for setting, name, registration in registrations:
        monkeypatch.setattr(module, name, registration, raising=False)
        monkeypatch.setattr(settings, setting, f"{task_runtime}:{name}")
    runtime = await build_runtime()
    try:
        user = await runtime.services.user.get_or_create_user(UserCreate(
            external_id=settings.DEFAULT_USER_ID, name=settings.DEFAULT_USER_NAME, email=settings.DEFAULT_USER_EMAIL,
        ))
        project = await runtime.services.project.create_project(user.id, ProjectCreate(
            name="Four plugin project", description="Synthetic coexistence", project_type="development",
        ))
        await runtime.services.memory.create_memory(user.id, MemoryCreate(
            title="Synthetic memory", content="One fact", context="Plugin test", keywords=["plugin"], tags=["test"],
        ))
        await runtime.services.entity.create_entity(user.id, EntityCreate(
            name="Synthetic organization", entity_type=EntityType.ORGANIZATION, tags=[],
        ))
        plan = (await runtime.services.plan.create_plan(user.id, PlanCreate(
            title="Synthetic plan", project_id=project.id, status=PlanStatus.ACTIVE,
        ))).plan
        task = (await runtime.services.task.create_task(user.id, TaskCreate(
            title="Synthetic task", plan_id=plan.id,
        ))).task
        assert task.plan_id == plan.id
        assert calls == ["memory", "entity", "plan", "task"]
        assert module.loads == 1
    finally:
        await dispose_runtime(runtime)
