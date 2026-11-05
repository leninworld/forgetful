"""Real REST and MCP adapters, policy failures and unchanged read shapes."""
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from httpx import ASGITransport, AsyncClient
from file_plugin_helpers import execute_tool, file_app
from file_plugin_helpers import file_runtime as file_runtime


@pytest.fixture
async def app(file_runtime, request, monkeypatch):
    if getattr(request, "param", True) is False:
        from app.config.settings import settings
        monkeypatch.setattr(settings, "FILE_WRITE_PLUGIN", "")
    app = file_app()
    async with Client(app) as client:
        async with AsyncClient(transport=ASGITransport(app=app.http_app(), raise_app_exceptions=False),
                               base_url="http://test") as http:
            yield client, http


def payload(name="plain.bin"):
    return {"filename": name, "description": "Transport fixture", "data": "YWJj", "mime_type": "text/plain"}


async def test_rest_mutations_warnings_read_and_missing(app):
    _, http = app
    created = await http.post("/api/v1/files", json=payload("warn.bin"))
    assert created.status_code == 201
    value = created.json()
    assert value["plugin_warnings"][0]["code"] == "FILE_POLICY"
    file_id = value["id"]
    updated = await http.put(f"/api/v1/files/{file_id}", json={"filename": "plain.bin"})
    assert updated.status_code == 200 and "plugin_warnings" not in updated.json()
    read = await http.get(f"/api/v1/files/{file_id}")
    assert read.status_code == 200 and "plugin_warnings" not in read.json()
    assert (await http.put(f"/api/v1/files/{file_id}", json={"filename": "blocked.bin"})).status_code == 400
    assert (await http.put(f"/api/v1/files/{file_id}", json={"filename": "error.bin"})).status_code == 503
    assert (await http.get(f"/api/v1/files/{file_id}")).json()["filename"] == "plain.bin"
    assert (await http.delete(f"/api/v1/files/{file_id}")).json() == {"success": True}
    assert (await http.delete(f"/api/v1/files/{file_id}")).status_code == 404


async def test_mcp_mutations_warnings_errors_and_deleted_id(app):
    client, _ = app
    made = await execute_tool(client, "create_file", payload())
    file_id = made["id"]
    warned = await execute_tool(client, "update_file", {"file_id": file_id, "filename": "warn.bin"})
    assert warned["plugin_warnings"][0]["code"] == "FILE_POLICY"
    with pytest.raises(ToolError, match="WRITE_PLUGIN_REJECTED"):
        await execute_tool(client, "update_file", {"file_id": file_id, "filename": "blocked.bin"})
    with pytest.raises(ToolError, match="WRITE_PLUGIN_UNAVAILABLE"):
        await execute_tool(client, "update_file", {"file_id": file_id, "filename": "error.bin"})
    removed = await execute_tool(client, "delete_file", {"file_id": file_id})
    assert removed["success"] is True and removed["deleted_id"] == file_id
    assert removed["plugin_warnings"]
    missing = await execute_tool(client, "delete_file", {"file_id": file_id})
    assert missing == {"success": False, "deleted_id": file_id}


@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_after_error_keeps_confirmed_file(app, transport):
    client, http = app
    data = payload("post_error.bin")
    if transport == "rest":
        response = await http.post("/api/v1/files", json=data)
        assert response.status_code == 201
        result = response.json()
    else:
        result = await execute_tool(client, "create_file", data)
    assert result["plugin_warnings"][0]["code"] == "FILE_PLUGIN_AFTER_SAVE_UNAVAILABLE"
    assert "SYNTHETIC_PRIVATE_ERROR" not in str(result)
    read = await execute_tool(client, "get_file", {"file_id": result["id"]})
    assert read["data"] == "YWJj" and "plugin_warnings" not in read


@pytest.mark.parametrize("app", [False], indirect=True)
async def test_no_plugin_flat_success_contracts(app):
    client, http = app
    created = await http.post("/api/v1/files", json=payload())
    assert created.status_code == 201 and "plugin_warnings" not in created.json()
    updated = await execute_tool(client, "update_file", {"file_id": created.json()["id"], "description": "Plain"})
    assert "plugin_warnings" not in updated and updated["data"] == "YWJj"
    removed = await execute_tool(client, "delete_file", {"file_id": created.json()["id"]})
    assert removed == {"success": True, "deleted_id": created.json()["id"]}


def test_response_wrapper_does_not_decode_again(monkeypatch):
    from datetime import UTC, datetime
    from app.models.file_models import File
    from app.models.file_plugin_models import FileWriteResult
    from app.routes.write_plugin_responses import FileWriteResponse
    saved = File(**payload(), id=1, size_bytes=3, created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
    result = FileWriteResult(file=saved)
    def forbidden(*args, **kwargs):
        raise AssertionError("Response wrapper decoded already validated content")
    monkeypatch.setattr("app.models.file_models.base64.b64decode", forbidden)
    output = FileWriteResponse.from_result(result).model_dump(mode="json")
    assert output == saved.model_dump(mode="json")


@pytest.mark.parametrize("app", [True, False], indirect=True)
async def test_core_project_errors_match_with_or_without_policy(app):
    client, http = app
    data = payload()
    data["project_id"] = 2147483647
    response = await http.post("/api/v1/files", json=data)
    assert response.status_code == 404 and response.json() == {"error": "Project 2147483647 not found"}
    saved = await execute_tool(client, "create_file", payload())
    response = await http.put(f"/api/v1/files/{saved['id']}", json={"project_id": 2147483647})
    assert response.status_code == 404 and response.json() == {"error": "File not found"}
    with pytest.raises(ToolError, match="Project 2147483647 not found"):
        await execute_tool(client, "create_file", data)
