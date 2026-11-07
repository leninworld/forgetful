"""Offline G0 for exact changed feature-controller paths, no project imports."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

P = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("selection_identity", P / "selection_identity.py")
recorder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recorder)
inside = ast.parse((P / "inside.py").read_text())
node = next(n for n in inside.body if isinstance(n, ast.FunctionDef) and n.name == "verify_expected_failure_crashes")
namespace = {}
exec(compile(ast.Module(body=[node], type_ignores=[]), "actual-feature-classifier", "exec"), namespace)
verify = namespace["verify_expected_failure_crashes"]
report = SimpleNamespace(nodeid="feature", when="call", outcome="failed", failed=True,
    longrepr=SimpleNamespace(reprcrash=SimpleNamespace(message="AssertionError: missing_bundle_tool_import_skill_bundle")))
recorder.pytest_runtest_logreport(report)
verify(recorder.reports, {"feature"})
rejected = 0
for message in ["AssertionError: other_reason", "RuntimeError: missing_bundle_tool_import_skill_bundle", None]:
    recorder.reports["feature"]["crash"] = message
    try:
        verify(recorder.reports, {"feature"})
    except AssertionError:
        rejected += 1
    else:
        raise AssertionError("Wrong cause accepted")
try:
    verify({}, {"feature"})
except KeyError:
    rejected += 1
else:
    raise AssertionError("Missing result accepted")
contract = json.loads((P / "baseline-regression-contract.json").read_text())
manifest = json.loads((P / "manifest.json").read_text())
failed = set(contract["expected_feature_assertion_failures"])
passed = set(contract["expected_passes"])
assert len(failed) == 2 and len(passed) == 1 and not failed & passed
assert failed | passed == set(manifest["regression_paths"])
# Execute the production overlay loop, including nested directory creation and hash checks.
host = ast.parse((P / "host.py").read_text())
loops = [node for node in ast.walk(host) if isinstance(node, ast.For)
         and isinstance(node.iter, ast.Call) and ast.unparse(node.iter).startswith("M['overlay_sha256']")]
assert len(loops) == 1
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder); packet = root / "packet"; runtime = root / "runtime"
    file = packet / "overlay/app/new/path.py"; file.parent.mkdir(parents=True); file.write_bytes(b"synthetic")
    digest = hashlib.sha256(b"synthetic").hexdigest()
    env = {"M": {"overlay_sha256": {"app/new/path.py": digest}}, "P": packet,
           "R": runtime, "hashlib": hashlib}
    compiled = compile(ast.Module(body=[loops[0]], type_ignores=[]), "actual-overlay-loop", "exec")
    exec(compiled, env)
    assert (runtime / "candidate/app/new/path.py").read_bytes() == b"synthetic"
    env["M"]["overlay_sha256"]["app/new/path.py"] = "0" * 64
    try:
        exec(compiled, env)
    except AssertionError:
        rejected += 1
    else:
        raise AssertionError("Bad overlay hash accepted")
source = Path("/source")
fields = ast.parse((source / "app/models/document_models.py").read_text())
document = next(n for n in fields.body if isinstance(n, ast.ClassDef) and n.name == "DocumentCreate")
field = next(n for n in document.body if isinstance(n, ast.AnnAssign) and n.target.id == "document_type")
assert ast.unparse(field.annotation) == "str | None"
classes = ast.parse((source / "app/bootstrap.py").read_text())
runtime = next(n for n in classes.body if isinstance(n, ast.ClassDef) and n.name == "Runtime")
assert {n.target.id for n in runtime.body if isinstance(n, ast.AnnAssign)} >= {"registry", "permitted_tools", "instance_scopes"}
errors = ast.parse((source / "app/exceptions.py").read_text())
not_found = next(n for n in errors.body if isinstance(n, ast.ClassDef) and n.name == "NotFoundError")
assert [ast.unparse(base) for base in not_found.bases] == ["Exception"]
for backend in ("sqlite", "postgres"):
    path = source / ("app/repositories/" + backend + "/" + backend + "_adapter.py")
    tree = ast.parse(path.read_text())
    methods = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    assert {"session", "system_session"} <= methods
    if backend == "sqlite":
        assert "PRAGMA foreign_keys=ON" in path.read_text()
# Migration heads include annotated assignments; never infer absent history.
revisions = set(); parents = set()
for file in (source / "alembic/versions").glob("*.py"):
    for node in ast.parse(file.read_text()).body:
        if isinstance(node, ast.Assign): targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign): targets, value = [node.target], node.value
        else: continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in ("revision", "down_revision"):
                item = ast.literal_eval(value)
                if target.id == "revision": revisions.add(item)
                elif isinstance(item, str): parents.add(item)
assert revisions - parents == {"20261008_plan_external_ref"}
# Exact baseline contracts used by migration, installed-wheel and shipping lanes.
import tomllib
migration_env = (source / "alembic/env.py").read_text()
assert "attributes" in migration_env and '"connection"' in migration_env
assert {n.name for n in classes.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))} >= {"build_runtime", "dispose_runtime"}
services = next(n for n in classes.body if isinstance(n, ast.ClassDef) and n.name == "Services")
assert {n.target.id for n in services.body if isinstance(n, ast.AnnAssign)} >= {"user", "skill", "document", "code_artifact", "file"}
wheel = tomllib.loads((source / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]["wheel"]
assert "alembic" in wheel.get("packages", []) or "alembic" in wheel.get("force-include", {}).values()
recipe = (source / "docker/Dockerfile").read_text()
assert recipe.count("FROM python:3.12-slim") == 1
assert recipe.count("COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv") == 1
assert rejected == 5
print(json.dumps({"status": "PASS", "actual_classifier": True,
    "passive_recorder": True, "actual_nested_overlay_loop": True,
    "wrong_cause_missing_hash_rejected": rejected,
    "migration_handoff": True, "runtime_services": True, "wheel_migrations": True,
    "shipping_recipe": True, "project_imports": False, "network": "none"}))
