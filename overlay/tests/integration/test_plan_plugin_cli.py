"""Real CLI parser, local executor and HTTP MCP remote executor contracts."""

import asyncio
import json

import pytest
from plan_plugin_helpers import loopback_plan_server, plan_app
from plan_plugin_helpers import plan_runtime as plan_runtime

from app.routes.cli.parser import dispatch


def cli_call(capsys, tool_name, arguments, *, server=None):
    argv = ["call", tool_name, "--args", json.dumps(arguments), "--json"]
    argv.extend(["--server", server] if server else ["--local"])
    code = dispatch(argv, serve_runner=None, reembed_runner=None)
    captured = capsys.readouterr()
    if code:
        errors = []
        for line in captured.err.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and set(value) == {"error"}:
                errors.append(value)
        assert len(errors) == 1, captured.err
        payload = errors[0]
    else:
        payload = json.loads(captured.out)
    return code, payload


def check_payload(code, payload, marker):
    assert code == (1 if marker in ("blocked", "error") else 0)
    if marker == "warn":
        assert payload["plugin_warnings"][0]["code"] == "PLAN_POLICY"
    elif marker == "plain":
        assert "plugin_warnings" not in payload
    else:
        assert ("PLAN_PLUGIN_REJECTED" if marker == "blocked" else "PLAN_PLUGIN_UNAVAILABLE") in str(payload)
        assert "SYNTHETIC_PRIVATE_ERROR" not in str(payload)


@pytest.mark.parametrize("marker", ["plain", "warn", "blocked", "error"])
def test_local_cli_plan_policy(plan_runtime, capsys, marker):
    code, project = cli_call(capsys, "create_project", {
        "name": "Local CLI plan", "description": "Real local parser", "project_type": "development",
    })
    assert code == 0
    code, payload = cli_call(capsys, "create_plan", {"project_id": project["id"], "title": marker})
    check_payload(code, payload, marker)
    if code == 0:
        read_code, read = cli_call(capsys, "get_plan", {"plan_id": payload["id"]})
        assert read_code == 0 and "plugin_warnings" not in read


@pytest.mark.parametrize("marker", ["plain", "warn", "blocked", "error"])
async def test_remote_cli_plan_policy_over_actual_http(plan_runtime, capsys, monkeypatch, marker):
    monkeypatch.setenv("FORGETFUL_TOKEN", "synthetic-test-only-not-a-credential")
    async with loopback_plan_server(plan_app()) as server:
        code, project = await asyncio.to_thread(cli_call, capsys, "create_project", {
            "name": "Remote CLI plan", "description": "Actual loopback HTTP MCP", "project_type": "development",
        }, server=server)
        assert code == 0
        code, payload = await asyncio.to_thread(cli_call, capsys, "create_plan", {
            "project_id": project["id"], "title": marker,
        }, server=server)
        check_payload(code, payload, marker)
        if code == 0:
            read_code, read = await asyncio.to_thread(cli_call, capsys, "get_plan", {"plan_id": payload["id"]}, server=server)
            assert read_code == 0 and "plugin_warnings" not in read
