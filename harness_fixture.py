"""Run inside the tiny approved Docker fixture before dependencies/models."""
import ctypes
import _ssl
import json
from pathlib import Path
import shutil

from file_oracles import validate_regression, verify_compatibility


def rejects(call):
    try:
        call()
    except (AssertionError, KeyError, TypeError):
        return
    raise AssertionError("Negative fixture unexpectedly accepted")


base = {"exitstatus": 1, "reports": {
    "fails": {"call": "failed", "setup": "passed", "teardown": "passed"},
    "control": {"call": "passed", "setup": "passed", "teardown": "passed"}}}
contract = {"expected_assertion_failures": ["fails"], "expected_passes": ["control"]}
validate_regression(base, {"fails": 1, "control": 1}, contract)
rejects(lambda: validate_regression(base, {"fails": 2, "control": 1}, contract))
for phase in ("setup", "call", "teardown"):
    wrong = json.loads(json.dumps(base))
    wrong["reports"]["control"][phase] = "failed"
    rejects(lambda: validate_regression(wrong, {"fails": 1, "control": 1}, contract))
record = {"kind": "null", "backend": "SQLite", "field": "project_id", "persisted": None, "error_type": None}
wanted = {("SQLite", "null", "project_id")}
verify_compatibility([record], [dict(record)], wanted)
rejects(lambda: verify_compatibility([record], [], wanted))
rejects(lambda: verify_compatibility([record], [record, record], wanted))
wrong = dict(record, persisted=4)
rejects(lambda: verify_compatibility([record], [wrong], wanted))
# A native library load proves executable work storage, not just mount arguments.
work = Path("/work")
work.mkdir(exist_ok=True)
native = work / "fixture-native.so"
shutil.copyfile(_ssl.__file__, native)
ctypes.CDLL(str(native))
mount = next(line for line in Path("/proc/self/mountinfo").read_text().splitlines() if line.split()[4] == "/work")
assert "noexec" not in mount.split()[5].split(",")

from execution_clock import resolve_clock
import hashlib
first = {"attempt_index": 1, "repair_authorized": False, "original_run": None,
         "target_seconds": 3000, "absolute_seconds": 3600}
assert resolve_clock(1000, first) == (1000.0, 0, 0)
original = {"attempt_index": 1, "github_run_id": "17", "job_start": 1000,
            "full_validation": False, "owned_stop": True, "network_rx_bytes": 12, "output_bytes": 30}
raw = json.dumps(original).encode()
second = dict(first, attempt_index=2, repair_authorized=True, original_run={
    "id": "17", "anchor_epoch": 1000, "result_sha256": hashlib.sha256(raw).hexdigest(),
    "reviewed_failure_kind": "candidate_assertion_failure"})
assert resolve_clock(1500, second, raw) == (1000.0, 12, 30)
rejects(lambda: resolve_clock(1500, dict(second, repair_authorized=False), raw))
rejects(lambda: resolve_clock(4000, second, raw))
rejects(lambda: resolve_clock(1500, second, raw + b"x"))
wrong = dict(second, original_run=dict(second["original_run"], anchor_epoch=1500))
rejects(lambda: resolve_clock(1500, wrong, raw))

print(json.dumps({"status": "HARNESS_FIXTURE_PASS", "same_production_oracles": True,
                  "negative_controls": 11, "native_load": True, "work_mount": mount}))

