"""Pure result validators used by both the production packet and its tiny fixture."""
from collections import Counter
import json

NULL_FIELDS = ("filename", "description", "mime_type", "data", "tags", "project_id", "source_repo",
               "source_files", "source_url", "confidence", "encoding_agent", "encoding_version",
               "agent_id", "agent_version", "agent_model")


def validate_regression(facts, counts, contract):
    failures = set(contract["expected_assertion_failures"])
    controls = set(contract["expected_passes"])
    assert not failures.intersection(controls)
    assert set(counts) == failures | controls and all(v == 1 for v in counts.values())
    assert facts["exitstatus"] == 1
    reports = facts["reports"]
    assert set(reports) == failures | controls
    assert all(reports[n].get("call") == "failed" for n in failures)
    assert all(reports[n].get("call") == "passed" for n in controls)
    assert all(row.get("setup") == row.get("teardown") == "passed" for row in reports.values())


def record_key(row):
    assert isinstance(row, dict)
    if row["kind"] == "null":
        return row["backend"], "null", row["field"]
    assert row["kind"] == "event"
    return row["backend"], "event", json.dumps(row["patch"], sort_keys=True)


def expected_keys():
    values = set()
    patches = [{}, {"description": "Changed"}, {"data": "eHl6"}, {"data": "Y!WJj"}]
    for backend in ("SQLite", "Postgres"):
        values.update((backend, "null", field) for field in NULL_FIELDS)
        values.update((backend, "event", json.dumps(patch, sort_keys=True)) for patch in patches)
    return values


def verify_compatibility(original, candidate, expected=None):
    wanted = expected_keys() if expected is None else expected
    def keyed(rows):
        assert all(isinstance(row, dict) for row in rows)
        counts = Counter(record_key(row) for row in rows)
        assert set(counts) == wanted and all(n == 1 for n in counts.values())
        return {record_key(row): row for row in rows}
    assert keyed(original) == keyed(candidate), "ORIGINAL_MAIN_COMPATIBILITY_MISMATCH"
