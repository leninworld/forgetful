"""Real source CLI parser and bounded CLI subprocess over local/HTTP execution."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from file_plugin_helpers import file_app, loopback_file_server
from file_plugin_helpers import file_runtime as file_runtime



def isolated_env(home):
    """Real platform lookups, all redirected below one synthetic home."""
    # Windows expands known folders from USERPROFILE and verifies they exist.
    for parts in (("AppData", "Local"), ("AppData", "Roaming")):
        home.joinpath(*parts).mkdir(parents=True, exist_ok=True)
    return dict(os.environ, HOME=str(home), USERPROFILE=str(home),
                XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
                XDG_CACHE_HOME=str(home / "cache"), XDG_STATE_HOME=str(home / "state"))


def assert_lookups_isolated(home):
    """Fail loudly if real platform lookups would escape the synthetic home."""
    probe = ("import json, platformdirs as p; print(json.dumps(["
             "p.user_data_dir('fastmcp', appauthor=False), "
             "p.user_data_dir('forgetful', ensure_exists=False), "
             "p.user_config_dir('forgetful', ensure_exists=False)]))")
    completed = subprocess.run(  # noqa: S603 - test-controlled arguments, no shell
        [sys.executable, "-c", probe], capture_output=True, text=True, encoding="utf-8",
        timeout=30, env=isolated_env(home), check=False,
    )
    assert completed.returncode == 0, completed.stderr
    root = home.resolve()
    for found in json.loads(completed.stdout):
        assert Path(found).resolve().is_relative_to(root), found


def remote_cli_call(server, home, tool_name, arguments):
    """Run the real CLI in its own bounded process; never abandon a worker thread."""
    env = isolated_env(home)
    completed = subprocess.run(  # noqa: S603 - test-controlled arguments, no shell
        [sys.executable, "-c", "import faulthandler; faulthandler.dump_traceback_later(20); from main import cli; raise SystemExit(cli())",
         "call", tool_name, "--args", json.dumps(arguments), "--json", "--server", server],
        capture_output=True, text=True, encoding="utf-8", timeout=30, env=env, check=False,
    )
    if completed.returncode:
        errors = []
        for line in completed.stderr.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and set(value) == {"error"}:
                errors.append(value)
        assert len(errors) == 1, completed.stderr
        payload = errors[0]
    else:
        payload = json.loads(completed.stdout)
    return completed.returncode, payload



@pytest.mark.parametrize("marker", ["plain", "warn", "blocked", "error"])
def test_remote_file_policy_over_actual_http(file_runtime, monkeypatch, tmp_path, marker):
    monkeypatch.setenv("FORGETFUL_TOKEN", "synthetic-test-only-not-a-credential")
    home = tmp_path / "http-cli-home"
    home.mkdir()
    assert_lookups_isolated(home)
    with loopback_file_server(file_app()) as server:
        code, result = remote_cli_call(server, home, "create_file", {
            "filename": marker + ".bin", "description": "Actual HTTP CLI",
            "mime_type": "application/octet-stream", "data": "YWJj",
        })
        assert code == (1 if marker in ("blocked", "error") else 0)
        if marker == "warn":
            assert result["plugin_warnings"][0]["code"] == "FILE_POLICY"
        elif marker == "plain":
            assert "plugin_warnings" not in result
        else:
            assert ("WRITE_PLUGIN_REJECTED" if marker == "blocked" else "WRITE_PLUGIN_UNAVAILABLE") in str(result)
            assert "SYNTHETIC_PRIVATE_ERROR" not in str(result)


@pytest.mark.parametrize("marker", ["plain", "warn", "blocked", "error"])
def test_local_file_policy_actual_subprocess(file_runtime, tmp_path, marker):
    home = tmp_path / "local-cli-home"
    home.mkdir()
    assert_lookups_isolated(home)
    env = isolated_env(home)
    env.update(DATABASE="SQLite", SQLITE_MEMORY="false", SQLITE_PATH=str(tmp_path / "child.db"),
               FILES_ENABLED="true", SKILLS_ENABLED="false", PLANNING_ENABLED="false",
               ACTIVITY_ENABLED="false", FILE_WRITE_PLUGIN=f"{file_runtime}:registration",
               FILE_WRITE_PLUGIN_MODE="enforce", MEMORY_WRITE_PLUGIN="", ENTITY_WRITE_PLUGIN="",
               RERANKING_ENABLED="false", FASTEMBED_LOCAL_FILES_ONLY="true",
               PYTHONPATH=str(tmp_path) + os.pathsep + str(Path.cwd()))
    completed = subprocess.run(  # noqa: S603 - bounded source CLI and synthetic arguments
        [sys.executable, "-c", "from main import cli; raise SystemExit(cli())",
         "call", "create_file", "--local", "--json", "--args", json.dumps({
             "filename": marker + ".bin", "description": "Actual local CLI",
             "mime_type": "application/octet-stream", "data": "YWJj",
         })], env=env, capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert completed.returncode == (1 if marker in ("blocked", "error") else 0), completed.stderr
    if not completed.returncode:
        result = json.loads(completed.stdout)
        assert result["filename"] == marker + ".bin"
        assert ("plugin_warnings" in result) == (marker == "warn")
    else:
        assert "SYNTHETIC_PRIVATE_ERROR" not in completed.stderr
