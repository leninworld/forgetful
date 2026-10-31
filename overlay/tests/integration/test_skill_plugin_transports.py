"""Skill policies through real REST, MCP registry and local CLI boundaries."""

from contextlib import asynccontextmanager
import json

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from httpx import ASGITransport, AsyncClient
from test_skill_plugin_runtime import skill_plugin_runtime as skill_plugin_runtime

from app.bootstrap import build_runtime, dispose_runtime
from app.config.settings import settings
from app.routes.api import skills
from app.routes.cli.parser import dispatch
from app.routes.mcp import meta_tools


def data(name="transport-skill", tags=None):
    return {"name": name, "description": "Synthetic workflow", "content": "Read the guide.",
            "tags": tags or []}


@pytest.fixture
async def skill_app(skill_plugin_runtime):
    @asynccontextmanager
    async def lifespan(app):
        runtime = await build_runtime()
        app.skill_service = runtime.services.skill
        app.user_service = runtime.services.user
        app.registry = runtime.registry
        app._instance_permitted_tools = runtime.permitted_tools
        app._instance_scopes = runtime.instance_scopes
        try:
            yield
        finally:
            await dispose_runtime(runtime)

    app = FastMCP("Skill policy workflow", lifespan=lifespan)
    meta_tools.register(app)
    skills.register(app)
    async with Client(app) as client:
        async with AsyncClient(transport=ASGITransport(app=app.http_app()), base_url="http://test") as http:
            yield client, http


async def test_rest_warning_rejection_unavailability_read_and_delete(skill_app):
    _, http = skill_app
    created = await http.post("/api/v1/skills", json=data(tags=["warn"]))
    assert created.status_code == 201
    saved = created.json()
    assert saved["name"] == "transport-skill"
    assert saved["plugin_warnings"][0]["message"] == "Review instructions."
    url = f"/api/v1/skills/{saved['id']}"
    read = await http.get(url)
    assert "plugin_warnings" not in read.json()
    for tags, status in [(["blocked"], 400), (["error"], 503)]:
        rejected = await http.put(url, json={"tags": tags})
        assert rejected.status_code == status
        assert "synthetic-secret" not in rejected.text
        assert (await http.get(url)).json()["tags"] == ["warn"]
    changed = await http.put(url, json={"tags": ["post-warn"]})
    assert changed.status_code == 200
    assert changed.json()["plugin_warnings"][0]["message"] == "Review saved skill."
    deleted = await http.delete(url)
    assert deleted.json() == {"success": True, "plugin_warnings": [
        {"outcome": "warn", "message": "Review saved skill.", "code": None},
    ]}
    assert (await http.get(url)).status_code == 404


async def test_registry_preserves_flat_shape_and_surfaces_policy_outcomes(skill_app):
    client, _ = skill_app

    async def execute(name, arguments):
        result = await client.call_tool("execute_forgetful_tool", {
            "tool_name": name, "arguments": arguments,
        })
        return result.data

    made = await execute("create_skill", data(tags=["warn"]))
    assert "skill" not in made and made["plugin_warnings"]
    with pytest.raises(ToolError, match="WRITE_PLUGIN_REJECTED: Skill rejected"):
        await execute("update_skill", {"skill_id": made["id"], "tags": ["blocked"]})
    with pytest.raises(ToolError, match="WRITE_PLUGIN_UNAVAILABLE: Skill Write Plugin unavailable"):
        await execute("update_skill", {"skill_id": made["id"], "tags": ["error"]})
    read = await execute("get_skill", {"skill_id": made["id"]})
    assert read["tags"] == ["warn"] and "plugin_warnings" not in read
    deleted = await execute("delete_skill", {"skill_id": made["id"]})
    assert deleted["success"] and deleted["deleted_id"] == made["id"]
    assert deleted["plugin_warnings"][0]["message"] == "Review instructions."


async def test_rest_import_dispatches_once_and_rejects_before_saving(skill_app, skill_plugin_runtime):
    import importlib

    _, http = skill_app
    module = importlib.import_module(skill_plugin_runtime)
    text = "---\nname: imported-transport\ndescription: Synthetic import\n---\nInstructions"
    response = await http.post("/api/v1/skills/import", json={"skill_md": text})
    assert response.status_code == 201
    assert module.calls == [("before", "create"), ("after", "create")]
    module.calls.clear()
    blocked = text.replace("imported-transport", "blocked-import").replace(
        "description: Synthetic import", "description: Synthetic import\ntags: [blocked]",
    )
    denied = await http.post("/api/v1/skills/import", json={"skill_md": blocked})
    assert denied.status_code == 400
    assert module.calls == [("before", "create")]
    assert (await http.get("/api/v1/skills")).json()["total"] == 1


@pytest.mark.parametrize("configured", [False, True])
async def test_empty_warnings_leave_existing_public_shape(skill_plugin_runtime, monkeypatch, configured):
    if not configured:
        monkeypatch.setattr(settings, "SKILL_WRITE_PLUGIN", "")
    runtime = await build_runtime()
    try:
        from app.routes.cli.local_executor import LocalExecutor

        executor = LocalExecutor(runtime)
        result = await executor.execute("create_skill", data())
        assert "plugin_warnings" not in result.model_dump(mode="json")
        deleted = await executor.execute("delete_skill", {"skill_id": result.id})
        assert deleted == {"success": True, "deleted_id": result.id}
    finally:
        await dispose_runtime(runtime)


@pytest.mark.parametrize("tags,expected", [(["warn"], 0), (["blocked"], 1), (["error"], 1)])
def test_local_cli_warning_and_error_shapes(skill_plugin_runtime, monkeypatch, tmp_path, capsys, tags, expected):
    monkeypatch.setattr(settings, "SQLITE_MEMORY", False)
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "skill-cli.db"))
    code = dispatch(["call", "create_skill", "--args", json.dumps(data(tags=tags)),
                     "--local", "--json"], serve_runner=None, reembed_runner=None)
    output = capsys.readouterr()
    assert code == expected
    if code == 0:
        assert json.loads(output.out)["plugin_warnings"][0]["message"] == "Review instructions."
    else:
        assert "WRITE_PLUGIN_" in output.err
        assert "synthetic-secret" not in output.err
