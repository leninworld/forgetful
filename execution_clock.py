"""Preserve the first execution anchor and cumulative counters across a reviewed repair."""
import hashlib
import json


def resolve_clock(job_started, manifest, original_bytes=None):
    attempt = manifest["attempt_index"]
    if attempt == 1:
        assert manifest.get("original_run") is None and not manifest.get("repair_authorized")
        return float(job_started), 0, 0
    assert attempt == 2 and manifest.get("repair_authorized"), "NO_REPAIR_AUTHORITY"
    pin = manifest["original_run"]
    assert original_bytes is not None
    assert hashlib.sha256(original_bytes).hexdigest() == pin["result_sha256"]
    original = json.loads(original_bytes)
    assert str(original["github_run_id"]) == str(pin["id"])
    assert original["attempt_index"] == 1 and not original["full_validation"]
    assert original["owned_stop"]
    assert pin["reviewed_failure_kind"] == "candidate_assertion_failure"
    start = float(original["job_start"])
    assert start == float(pin["anchor_epoch"]), "ORIGINAL_ANCHOR_MISMATCH"
    assert start <= job_started < start + manifest["target_seconds"], "REPAIR_WINDOW_EXPIRED"
    return start, int(original["network_rx_bytes"]), int(original["output_bytes"])
