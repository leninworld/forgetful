"""Passive pytest evidence: records selection and outcomes, never changes them."""
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path

selected = []
deselected = []
reports = {}


def pytest_collection_finish(session):
    selected.extend(item.nodeid for item in session.items)


def pytest_deselected(items):
    deselected.extend(item.nodeid for item in items)


def pytest_runtest_logreport(report):
    reports.setdefault(report.nodeid, {})[report.when] = report.outcome


def pytest_sessionfinish(session, exitstatus):
    rows = defaultdict(lambda: {'selected': [], 'deselected': [], 'executed': [], 'skipped': []})
    for key, values in [('selected', selected), ('deselected', deselected)]:
        for node in values:
            rows[node.split('::', 1)[0]][key].append(node)
    for node, values in reports.items():
        row = rows[node.split('::', 1)[0]]
        if values.get('call') in ('passed', 'failed'):
            row['executed'].append(node)
        if 'skipped' in values.values():
            row['skipped'].append(node)
    summary = {}
    for name, values in rows.items():
        summary[name] = {key: len(set(nodes)) for key, nodes in values.items()}
        summary[name]['nodeids_sha256'] = {
            key: hashlib.sha256('\n'.join(sorted(set(nodes))).encode()).hexdigest()
            for key, nodes in values.items()}
    Path(os.environ['SELECTION_EVIDENCE_FILE']).write_text(json.dumps({
        'exitstatus': int(exitstatus), 'files': summary,
        'selected_nodeids': selected, 'deselected_nodeids': deselected,
        'reports': reports}, indent=2))
