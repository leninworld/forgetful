"""Task policies through the actual local CLI and real HTTP remote CLI."""

import importlib

import httpx
import pytest
from plan_plugin_helpers import loopback_plan_server, plan_app
from task_plugin_helpers import operation_call, seed_sync
from task_plugin_helpers import task_runtime as task_runtime
from test_plan_plugin_cli import assert_lookups_isolated, cli_call, remote_cli_call

OPERATIONS = ("create", "update", "claim", "transition", "criterion_add", "criterion_update",
              "criterion_delete", "dependency_add", "dependency_remove")


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("outcome,code", [("reject", "TASK_PLUGIN_REJECTED"), ("error", "TASK_PLUGIN_UNAVAILABLE")])
def test_local_cli_every_supported_task_mutation(task_runtime, capsys, operation, outcome, code):
    def call(tool, arguments):
        status, payload = cli_call(capsys, tool, arguments)
        assert status == 0, payload
        return payload

    seed = seed_sync(call, operation)
    module = importlib.import_module(task_runtime)
    module.outcome = outcome
    module.before_calls.clear()
    module.after_calls.clear()
    tool, arguments, _, _ = operation_call(operation, seed)
    status, payload = cli_call(capsys, tool, arguments)
    assert status == 1 and code in str(payload)
    assert "SYNTHETIC_PRIVATE_TASK_ERROR" not in str(payload)
    assert [r.operation for r in module.before_calls] == [operation]
    assert module.after_calls == []


def rest_seed_call(client, tool, arguments):
    if tool == "create_project":
        method, path, body = "POST", "/api/v1/projects", arguments
    elif tool == "create_plan":
        method, path, body = "POST", "/api/v1/plans", arguments
    elif tool == "create_task":
        method, path, body = "POST", "/api/v1/tasks", arguments
    elif tool == "transition_task":
        method, path = "POST", f"/api/v1/tasks/{arguments['task_id']}/transition"
        body = {k: v for k, v in arguments.items() if k != "task_id"}
    elif tool == "add_criterion":
        method, path, body = "POST", f"/api/v1/tasks/{arguments['task_id']}/criteria", {"description": arguments["description"]}
    else:
        assert tool == "add_dependency"
        method, path, body = "POST", f"/api/v1/tasks/{arguments['task_id']}/dependencies", {"depends_on_task_id": arguments["depends_on_task_id"]}
    response = client.request(method, path, json=body)
    assert response.is_success, response.text
    return response.json()


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("outcome,code", [("reject", "TASK_PLUGIN_REJECTED"), ("error", "TASK_PLUGIN_UNAVAILABLE")])
def test_remote_cli_every_supported_task_mutation(task_runtime, monkeypatch, tmp_path, operation, outcome, code):
    monkeypatch.setenv("FORGETFUL_TOKEN", "synthetic-test-only-not-a-credential")
    home = tmp_path / "task-remote-cli-home"
    home.mkdir()
    assert_lookups_isolated(home)
    with loopback_plan_server(plan_app()) as server:
        # Seed via real REST so each case needs only one bounded remote CLI process.
        with httpx.Client(base_url=server, timeout=15) as client:
            seed = seed_sync(lambda tool, arguments: rest_seed_call(client, tool, arguments), operation)
        module = importlib.import_module(task_runtime)
        module.outcome = outcome
        module.before_calls.clear()
        module.after_calls.clear()
        tool, arguments, _, _ = operation_call(operation, seed)
        status, payload = remote_cli_call(server, home, tool, arguments)
        assert status == 1 and code in str(payload)
        assert "SYNTHETIC_PRIVATE_TASK_ERROR" not in str(payload)
        assert [r.operation for r in module.before_calls] == [operation]
        assert module.after_calls == []


def test_local_cli_flat_task_warning(task_runtime, capsys):
    def call(tool, arguments):
        code, payload = cli_call(capsys, tool, arguments)
        assert code == 0, payload
        return payload

    seed = seed_sync(call, "create")
    module = importlib.import_module(task_runtime)
    module.outcome = "warn"
    module.after_warning = True
    code, payload = cli_call(capsys, "create_task", {"title": "Warned task", "plan_id": seed["plan"]["id"]})
    assert code == 0 and "task" not in payload
    assert [w["code"] for w in payload["plugin_warnings"]] == ["TASK_POLICY", "TASK_AFTER"]


def test_remote_cli_keeps_flat_nonempty_task_warnings(task_runtime, monkeypatch, tmp_path):
    monkeypatch.setenv("FORGETFUL_TOKEN", "synthetic-test-only-not-a-credential")
    home = tmp_path / "task-warning-remote-cli-home"
    home.mkdir()
    assert_lookups_isolated(home)
    with loopback_plan_server(plan_app()) as server:
        with httpx.Client(base_url=server, timeout=15) as client:
            seed = seed_sync(lambda tool, arguments: rest_seed_call(client, tool, arguments), "create")
        module = importlib.import_module(task_runtime)
        module.outcome = "warn"
        module.after_warning = True
        status, payload = remote_cli_call(server, home, "create_task", {
            "title": "Remote warned task", "plan_id": seed["plan"]["id"],
        })
        assert status == 0 and "task" not in payload
        assert [w["code"] for w in payload["plugin_warnings"]] == ["TASK_POLICY", "TASK_AFTER"]
