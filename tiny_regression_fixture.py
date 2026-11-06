"""Tiny offline check of the exact changed passive evidence path; no project imports."""
import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

P = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("selection_identity", P / "selection_identity.py")
recorder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recorder)
tree = ast.parse((P / "inside.py").read_text())
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "verify_expected_failure_crashes")
namespace = {}
exec(compile(ast.Module(body=[node], type_ignores=[]), "exact-failure-classifier", "exec"), namespace)
verify = namespace["verify_expected_failure_crashes"]

def record(name, message):
    report = SimpleNamespace(nodeid=name, when="call", outcome="failed", failed=True,
                             longrepr=SimpleNamespace(reprcrash=SimpleNamespace(message=message)))
    recorder.pytest_runtest_logreport(report)

record("race", "AssertionError: expected plan_auto_completion_unavailable, got PLAN_AFTER")
record("child", "asyncio.exceptions.CancelledError")
verify(recorder.reports, {"race"}, {"child"})
assert recorder.reports["race"]["call"] == "failed"
positive = 1
negative = 0
for target, message in [("race", "AssertionError"), ("race", "RuntimeError: plan_auto_completion_unavailable"),
                        ("child", "ValueError"), ("child", None)]:
    original = recorder.reports[target]["crash"]
    recorder.reports[target]["crash"] = message
    try:
        verify(recorder.reports, {"race"}, {"child"})
    except AssertionError:
        negative += 1
    else:
        raise AssertionError("Wrong-cause failure was accepted")
    finally:
        recorder.reports[target]["crash"] = original
original = recorder.reports.pop("race")
try:
    verify(recorder.reports, {"race"}, {"child"})
except KeyError:
    negative += 1
else:
    raise AssertionError("Missing evidence was accepted")
recorder.reports["race"] = original
contract = json.loads((P / "baseline-regression-contract.json").read_text())
manifest = json.loads((P / "manifest.json").read_text())
a, b, c = (set(contract[key]) for key in ("expected_race_assertion_failures", "expected_child_cancel_failures", "expected_passes"))
assert len(a) == len(b) == len(c) == 4 and not (a & b or a & c or b & c)
assert a | b | c == set(manifest["regression_paths"])
print(json.dumps({"status": "PASS", "positive": positive, "wrong_cause_or_missing_rejected": negative,
                  "actual_classifier_extracted": True, "passive_recorder_exercised": True, "network": "none", "project_imports": False}))
