"""Proposed complete #171 container lanes. Not authorized until packet review."""
import ast
import atexit
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
import zipfile

P = Path('/packet')
R = Path('/runtime')
M = json.loads((P / 'manifest.json').read_text())
MODE = sys.argv[1]
def resource_snapshot(label):
    group = Path('/sys/fs/cgroup')
    stat_fields = dict(line.split() for line in (group / 'memory.stat').read_text().splitlines())
    current = int((group / 'memory.current').read_text())
    inactive_file = int(stat_fields.get('inactive_file', 0))
    facts = {'phase': MODE, 'at': time.time(),
             'working_set': max(0, current - inactive_file), 'inactive_file': inactive_file,
             'current': current,
             'peak': int((group / 'memory.peak').read_text()),
             'limit': int((group / 'memory.max').read_text()), 'mounts': {}}
    for mount in ('/tmp', '/work', '/runtime', '/source'):
        stat = os.statvfs(mount)
        facts['mounts'][mount] = dict(free=stat.f_bavail * stat.f_frsize,
                                     inodes=stat.f_files, available_inodes=stat.f_favail)
    (R / (MODE + '-resources-' + label + '.json')).write_text(json.dumps(facts, indent=2))


resource_snapshot('before')
atexit.register(resource_snapshot, 'after')
ROOT = Path('/work/source')
shutil.copytree('/source', ROOT)
os.chdir(ROOT)
ENV = dict(os.environ, PYTHONPATH=str(ROOT) + ':/work/rootpkg:/packet',
           PATH='/work/rootpkg/bin:' + os.environ['PATH'], PYTHONDONTWRITEBYTECODE='1')
assert platform.python_version().startswith('3.13.')
assert platform.machine() == 'x86_64'
assert hashlib.sha256((ROOT / 'uv.lock').read_bytes()).hexdigest() == M['lock_sha256']
STEPS = []


class LaneFailure(Exception):
    pass


class HarnessFailure(Exception):
    pass


def run(name, args, *, expected=0, cwd=ROOT, env=ENV, seconds):
    path = R / (MODE + '-' + name + '.log')
    begin = time.monotonic()
    remaining = float(os.environ['VALIDATION_PHASE_DEADLINE_EPOCH']) - time.time() - 10
    if remaining <= 0:
        raise HarnessFailure('Global attempt deadline reached', name)
    limit = min(seconds, remaining)
    timed_out = False
    with path.open('x') as stream:
        process = subprocess.Popen(args, cwd=cwd, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
                code = process.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                code = process.wait()
    accepted = (expected,) if isinstance(expected, int) else expected
    row = dict(name=name, exit=code, elapsed=time.monotonic() - begin,
               timeout_seconds=limit, timed_out=timed_out,
               status='TIMEOUT' if timed_out else ('PASS' if code in accepted else 'FAIL'))
    STEPS.append(row)
    print(json.dumps(row), flush=True)
    if timed_out or code not in accepted:
        raise LaneFailure(name, code, 'timeout' if timed_out else 'exit')
    return row['elapsed']


def fingerprint(tree):
    return {name: hashlib.sha256((tree / name).read_bytes()).hexdigest()
            for name in M['overlay_sha256']}


def normalized_ast(raw):
    """Compare code except import grouping/order; no app imports or evaluation."""
    tree = ast.parse(raw)
    def visit(node):
        if not isinstance(node, ast.AST):
            return node
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                rebuilt = []
                block = []
                def flush():
                    if block:
                        rebuilt.extend(sorted(block, key=repr))
                        block.clear()
                for child in value:
                    if isinstance(child, ast.Import):
                        block.extend(('import', a.name, a.asname) for a in child.names)
                    elif isinstance(child, ast.ImportFrom):
                        block.extend(('from', child.level, child.module, a.name, a.asname)
                                     for a in child.names)
                    else:
                        flush()
                        rebuilt.append(visit(child))
                flush()
                setattr(node, field, rebuilt)
            elif isinstance(value, ast.AST):
                setattr(node, field, visit(value))
        return node
    # repr of the normalized dump is an integrity check, not an import-side-effect proof.
    return ast.dump(visit(tree), include_attributes=False)


def verify_expected_failure_crashes(reports, races, cancellations):
    assert all(isinstance(reports[node].get('crash'), str)
               and reports[node]['crash'].startswith('AssertionError')
               and 'plan_auto_completion_unavailable' in reports[node]['crash'] for node in races)
    assert all(isinstance(reports[node].get('crash'), str)
               and 'CancelledError' in reports[node]['crash'] for node in cancellations)


def pytest_lane(name, paths, marker='not e2e', regression=False):
    evidence = R / (MODE + '-' + name + '-selection.json')
    candidate_lane = MODE in ('candidate', 'postgres', 'image')
    error = None
    begin = time.monotonic()
    try:
        run(name, [sys.executable, '-m', 'pytest', '-p', 'selection_identity',
                   '-p', 'no:cacheprovider', *paths, '-m', marker,
                   *(['-vv', '-o', 'faulthandler_timeout=20'] if candidate_lane else ['-q']),
                   *(['--continue-on-collection-errors'] if candidate_lane else []),
                   *(['-p', 'image_identity_plugin'] if MODE == 'image' else []),
                   '--basetemp=/tmp/pytest-' + MODE + '-' + name,
                   '--junitxml=' + str(R / (MODE + '-' + name + '.xml'))],
            env=dict(ENV, SELECTION_EVIDENCE_FILE=str(evidence)),
            seconds=M['pytest_timeout_seconds'][MODE])
    except LaneFailure as exc:
        error = exc
    command = STEPS[-1]
    interrupted = command.get('timed_out', False) or (
        isinstance(command.get('exit'), int) and command['exit'] < 0
    )
    if interrupted:
        outcome = dict(mode=MODE, lane=name, candidate=candidate_lane,
                       status='INTERRUPTED', coverage='UNKNOWN',
                       problems=['LANE_INTERRUPTED'], lost_baseline_ids=None,
                       actual_exitstatus=command.get('exit'),
                       selection_evidence_present=evidence.is_file(),
                       command_error=str(error) if error else None)
        (R / (MODE + '-' + name + '-outcome.json')).write_text(json.dumps(outcome, indent=2))
        failure = LaneFailure if candidate_lane else HarnessFailure
        raise failure(name, 'INTERRUPTED', 'coverage UNKNOWN')
    problems = []
    if not evidence.is_file():
        problems.append('MISSING_SELECTION_EVIDENCE')
        facts = {'selected_nodeids': [], 'reports': {}, 'exitstatus': None, 'collection_reports': []}
    else:
        facts = json.loads(evidence.read_text())
    reports = facts['reports']
    counts = Counter(facts['selected_nodeids'])
    if any(row.get('setup') == 'failed' or row.get('teardown') == 'failed' for row in reports.values()):
        problems.append('SETUP_OR_TEARDOWN_FAILURE')
    if facts['exitstatus'] not in (0, 1) or any(row['outcome'] == 'failed' for row in facts.get('collection_reports', [])):
        problems.append('COLLECTION_OR_INTERNAL_FAILURE')
    if any(count != 1 for count in counts.values()):
        problems.append('DUPLICATE_SELECTED_IDS')
    for path in paths:
        if not any((node == path or node.startswith(path.rstrip('/') + '/') or node.startswith(path + '::'))
                   and row.get('call') in ('passed', 'failed') for node, row in reports.items()):
            problems.append('EMPTY_OR_ENTIRELY_SKIPPED_PATH: ' + path)
    if MODE in ('candidate', 'postgres'):
        base = 'baseline' if MODE == 'candidate' else 'baseline-postgres'
        original = json.loads((R / (base + '-full-selection.json')).read_text())
        missing = list((Counter(original['selected_nodeids']) - counts).elements())
        if missing:
            problems.append('LOST_BASELINE_IDS')
    else:
        missing = []
    outcome = dict(mode=MODE, lane=name, candidate=candidate_lane, problems=problems,
                   lost_baseline_ids=missing, actual_exitstatus=facts['exitstatus'],
                   selection_evidence_present=evidence.is_file(), command_error=str(error) if error else None)
    (R / (MODE + '-' + name + '-outcome.json')).write_text(json.dumps(outcome, indent=2))
    if problems:
        failure = LaneFailure if candidate_lane else HarnessFailure
        raise failure(name, problems)
    if regression:
        contract = json.loads((P / 'baseline-regression-contract.json').read_text())
        races = set(contract['expected_race_assertion_failures'])
        cancellations = set(contract['expected_child_cancel_failures'])
        failed = races | cancellations
        passed = set(contract['expected_passes'])
        assert set(counts) == failed | passed
        assert facts['exitstatus'] == 1
        assert all(reports[node].get('call') == 'failed' for node in failed)
        verify_expected_failure_crashes(reports, races, cancellations)
        assert all(reports[node].get('call') == 'passed' for node in passed)
        assert all(row.get('setup') == row.get('teardown') == 'passed' for row in reports.values())
        STEPS[-1]['status'] = 'EXPECTED_BEHAVIOR_FAILURES_VERIFIED'
    elif error is not None:
        raise error
    return time.monotonic() - begin


def lane(name, function):
    try:
        function()
    except (LaneFailure, AssertionError) as exc:
        # Candidate assertions, setup and collection failures expose other independent lanes; never edit/retry.
        STEPS.append(dict(name=name, status='FAIL', error=str(exc)))


if MODE == 'normalize':
    assert fingerprint(ROOT) == M['overlay_sha256']
    files = [name for name in M['overlay_sha256'] if name.endswith('.py')]
    before = {name: (ROOT / name).read_text() for name in files}
    run('style-only', ['ruff', 'check', '--fix', '--select', 'I,COM,Q', '--no-cache', *files], seconds=60)
    assert all(normalized_ast(before[name]) == normalized_ast((ROOT / name).read_text()) for name in files)
    facts = fingerprint(ROOT)
    for name in facts:
        dest = R / 'normalized-source' / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, dest)
    (R / 'tested-hashes.json').write_text(json.dumps(facts, indent=2))
    (R / 'normalize-result.json').write_text(json.dumps(dict(status='PASS', steps=STEPS)))
    raise SystemExit(0)

if MODE == 'docker-preflight':
    assert fingerprint(ROOT) == json.loads((R / 'tested-hashes.json').read_text())
    run('docker-version', ['docker', 'version'], seconds=10)
    run('docker-linkage', ['ldd', '/usr/local/bin/docker'], expected=(0, 1), seconds=10)
    run('owned-pg-inspect', ['docker', 'inspect', '--format', '{{.Id}}', 'forgetful-db'], seconds=10)
    actual = (R / 'docker-preflight-owned-pg-inspect.log').read_text().strip()
    assert actual == os.environ['EXPECTED_POSTGRES_ID']
    (R / 'docker-preflight-result.json').write_text(json.dumps(dict(status='PASS', steps=STEPS)))
    raise SystemExit(0)


candidate = MODE in ('candidate', 'postgres', 'image')
if candidate:
    TESTED = json.loads((R / 'tested-hashes.json').read_text())
    assert fingerprint(ROOT) == TESTED
if MODE == 'regression':
    for name in M['regression_files']:
        dest = ROOT / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(R / 'normalized-source' / name, dest)

if MODE == 'candidate':
    # Lint the frozen source before the build creates ignored app/_version.py.
    lane('ruff-full', lambda: run('ruff-full', ['ruff', 'check', '--no-cache', '.'], seconds=60))

dist = R / ('baseline-dist' if MODE.startswith('baseline') or MODE == 'regression' else 'dist')
try:
    if not dist.exists():
        run('root-build', ['uv', 'build', '--offline', '--no-build-isolation',
                          '--python', '/buildenv/bin/python', '--out-dir', str(dist)],
            env=dict(ENV, SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0+plan171'), seconds=120)
    wheel = next(dist.glob('*.whl'))
    run('root-install', ['uv', 'pip', 'install', '--offline', '--no-deps',
                         '--target', '/work/rootpkg', str(wheel)], seconds=30)
    run('git-preflight', ['git', '--version'], seconds=10)
    if MODE in ('baseline-postgres', 'postgres', 'image'):
        run('docker-cli-preflight', ['docker', '--version'], seconds=10)
    run('console-preflight', ['forgetful', '--help'], seconds=30)
    run('models-before', [sys.executable, '/packet/cache_identity.py', '/models'], seconds=60)
except LaneFailure as exc:
    raise HarnessFailure('Root install/preflight failure is terminal', str(exc)) from exc

source_paths = ['tests/architecture', 'tests/integration', 'tests/e2e_sqlite', 'tests/e2e_provider_sessions']
native_paths = ['tests/e2e', 'tests/e2e_provider_sessions']
if MODE in ('baseline', 'baseline-postgres'):
    if MODE == 'baseline-postgres':
        run('pg-client', ['pg_dump', '--version'], seconds=10)
    elapsed = pytest_lane('full', native_paths if MODE.endswith('postgres') else source_paths,
                          marker='e2e' if MODE.endswith('postgres') else 'not e2e')
    (R / (MODE + '-result.json')).write_text(json.dumps(dict(status='CALIBRATION_COMPLETE', tests_seconds=elapsed, steps=STEPS)))
elif MODE == 'regression':
    pytest_lane('behavior', M['regression_paths'], regression=True)
    (R / 'regression-result.json').write_text(json.dumps(dict(status='PASS', steps=STEPS)))
else:
    assert MODE in ('candidate', 'postgres', 'image')
    if MODE == 'candidate':
        lane('repair-focused', lambda: run('repair-focused', [sys.executable, '-m', 'pytest',
             'tests/integration/test_plan_plugin_cli.py',
             'tests/integration/test_plan_plugin_transports.py::test_memory_entity_and_plan_plugins_coexist',
             '-q', '-p', 'no:cacheprovider'], seconds=180))
        lane('full-source', lambda: pytest_lane('full', source_paths))
        def installed():
            with zipfile.ZipFile(wheel) as archive:
                names = archive.namelist()
                for name in ('plan_plugin_models.py', 'plan_plugin_protocol.py', 'plan_write_plugin_service.py', 'plan_plugin_responses.py'):
                    if not any(item.endswith('/' + name) for item in names):
                        raise LaneFailure('MISSING_INSTALLED_MODULE', name)
                assert any('/versions/' in item and 'alembic' in item for item in names)
            run('installed-wheel', ['uv', 'pip', 'install', '--offline', '--no-deps', '--target', '/work/installed', str(wheel)], seconds=30)
            demo = Path('/work/demo')
            demo.mkdir()
            run('installed-demo', [sys.executable, '/packet/installed_demo.py'], cwd=demo,
                env=dict(ENV, PYTHONPATH='/work/installed:/packet'), seconds=120)
            run('installed-console', [sys.executable, '/work/installed/bin/forgetful', '--help'], cwd=demo,
                env=dict(ENV, PYTHONPATH='/work/installed:/packet'), seconds=30)
        lane('installed', installed)
    elif MODE == 'postgres':
        run('pg-client', ['pg_dump', '--version'], seconds=10)
        lane('full-native', lambda: pytest_lane('full', native_paths, marker='e2e'))
    else:
        lane('shipping', lambda: pytest_lane('shipping', ['tests/docker']))
    run('models-after', [sys.executable, '/packet/cache_identity.py', '/models'], seconds=60)
    assert fingerprint(ROOT) == TESTED
    status = 'FAIL' if any(step['status'] == 'FAIL' for step in STEPS) else 'PASS'
    (R / (MODE + '-result.json')).write_text(json.dumps(dict(status=status, tested_hashes=TESTED, steps=STEPS), indent=2))
    raise SystemExit(0 if status == 'PASS' else 1)
