"""Complete Resource List Pagination215 lanes; execute only after exact approval."""
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
if MODE.startswith('regression-red-'):
    backend = MODE.rsplit('-', 1)[1]
    regression = M['pagination_regressions'][backend]
    assert M['pagination_regressions']['frozen'] and regression['paths']
    # Inject exactly the normalized tests exercised by the candidate. Never replace
    # green source with raw overlay bytes after the approved style-only phase.
    tested = json.loads((R / 'tested-hashes.json').read_text())
    for name in regression['paths']:
        assert name.startswith('tests/') and '..' not in Path(name).parts
        raw = (R / 'normalized-source' / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == tested[name]
        dest = ROOT / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
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
    unreaped = False
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
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    code = None
                    unreaped = True
    accepted = (expected,) if isinstance(expected, int) else expected
    row = dict(name=name, exit=code, elapsed=time.monotonic() - begin,
               timeout_seconds=limit, timed_out=timed_out, unreaped=unreaped,
               status='TIMEOUT' if timed_out else ('PASS' if code in accepted else 'FAIL'))
    STEPS.append(row)
    print(json.dumps(row), flush=True)
    if timed_out or code not in accepted:
        raise LaneFailure(name, code, 'timeout' if timed_out else 'exit')
    return row['elapsed']


def fingerprint(tree):
    return {name: hashlib.sha256((tree / name).read_bytes()).hexdigest()
            for name in M['overlay_sha256']}


def classify_regression(facts, expected_ids, expected_failures):
    """Production classifier shared by real regression and early real-pytest controls."""
    expected_ids, expected_failures = set(expected_ids), set(expected_failures)
    assert expected_ids and expected_failures <= expected_ids
    assert len(facts['selected_nodeids']) == len(expected_ids)
    assert set(facts['selected_nodeids']) == expected_ids, 'REGRESSION_WRONG_IDS'
    assert not facts['collection_reports'], 'REGRESSION_COLLECTION_OR_SKIP_UNKNOWN'
    assert facts['exitstatus'] == (1 if expected_failures else 0), 'REGRESSION_WRONG_EXIT'
    reports = facts['reports']
    assert set(reports) == expected_ids
    for node in expected_ids:
        row = reports[node]
        assert row.get('setup') == row.get('teardown') == 'passed', 'REGRESSION_FIXTURE_FAILURE'
        assert row.get('call') == ('failed' if node in expected_failures else 'passed')
        if node in expected_failures:
            assert isinstance(row.get('crash'), str) and row['crash'].startswith(('AssertionError', 'assert ')), 'NON_ASSERTION_RED_FAILURE'
    return {'classification': 'KNOWN_ASSERTION_RED' if expected_failures else 'EXACT_REGRESSION_GREEN',
            'selected': sorted(expected_ids), 'expectedFailures': sorted(expected_failures)}


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





def pytest_lane(name, paths, marker='not e2e'):
    evidence = R / (MODE + '-' + name + '-selection.json')
    candidate_lane = MODE in ('candidate', 'postgres', 'image', 'early-sqlite', 'early-postgres', 'controller-fixture')
    if MODE in ('diagnosed-sqlite', 'diagnosed-postgres'):
        candidate_lane = True
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
    if MODE in ('candidate', 'postgres') and name == 'full':
        base = 'baseline' if MODE == 'candidate' else 'baseline-postgres'
        original = json.loads((R / (base + '-full-selection.json')).read_text())
        missing = list((Counter(original['selected_nodeids']) - counts).elements())
        if missing:
            problems.append('LOST_BASELINE_IDS')
        backend = 'sqlite' if MODE == 'candidate' else 'postgres'
        for node in M['candidate_required_nodeids'][backend]:
            if reports.get(node, {}).get('call') != 'passed':
                problems.append('MISSING_OR_NONPASS_REQUIRED_PAGINATION_CASE: ' + node)
    else:
        missing = []
    outcome = dict(mode=MODE, lane=name, candidate=candidate_lane, problems=problems,
                   lost_baseline_ids=missing, actual_exitstatus=facts['exitstatus'],
                   selection_evidence_present=evidence.is_file(), command_error=str(error) if error else None)
    (R / (MODE + '-' + name + '-outcome.json')).write_text(json.dumps(outcome, indent=2))
    if problems:
        failure = LaneFailure if candidate_lane else HarnessFailure
        raise failure(name, problems)
    if error is not None:
        raise error
    return time.monotonic() - begin


def lane(name, function):
    try:
        function()
    except (LaneFailure, AssertionError) as exc:
        # Candidate assertions, setup and collection failures expose other independent lanes; never edit/retry.
        STEPS.append(dict(name=name, status='FAIL', error=str(exc)))


def ruff_findings(raw, root):
    rows = json.loads(raw)
    assert isinstance(rows, list)
    result = Counter()
    for row in rows:
        name = str(Path(row['filename']).resolve().relative_to(root.resolve()))
        assert isinstance(row['code'], str) and isinstance(row['message'], str)
        result[(name, row['code'], row['message'])] += 1
    return result


def compare_ruff(candidate, baseline, changed):
    assert not any(name in changed for name, code, message in candidate), 'CHANGED_FILE_LINT_FINDING'
    assert not candidate - baseline, 'NEW_OR_MULTIPLIED_LINT_FINDING'


def ruff_gate(label):
    binary = shutil.which('ruff')
    assert binary and Path(binary).is_file()
    run(label + '-version', [binary, '--version'], seconds=10)
    if MODE == 'normalize':
        # Actual Ruff dependency plus realistic success/new/error/multiplicity cases.
        a = Path('/work/ruff-fixture-base')
        b = Path('/work/ruff-fixture-candidate')
        a.mkdir(); b.mkdir()
        for root in (a, b):
            (root / 'example.py').write_text('import os\n')
        def fixture(name, root):
            run(name, [binary, 'check', '--isolated', '--select', 'F401', '--no-cache',
                       '--output-format', 'json', '.'], cwd=root, expected=(0, 1), seconds=10)
            return ruff_findings((R / (MODE + '-' + name + '.log')).read_text(), root)
        old = fixture('ruff-fixture-baseline', a)
        same = fixture('ruff-fixture-identical', b)
        assert old and same == old
        compare_ruff(same, old, set())
        rejected = 0
        (b / 'example.py').write_text('import os\nimport sys\n')
        new = fixture('ruff-fixture-new', b)
        for current, changed in ((new, set()), (same, {'example.py'}), (same + same, set())):
            try:
                compare_ruff(current, old, changed)
            except AssertionError:
                rejected += 1
            else:
                raise AssertionError('Ruff comparator accepted a new/changed/multiplied violation')
        assert rejected == 3
        (R / 'ruff-comparator-fixture.json').write_text(json.dumps(dict(
            status='PASS', actual_ruff=binary, inherited_identical_accepted=True,
            new_changed_and_multiplicity_rejected=rejected)))
    findings = {}
    for name, root in [('baseline', Path('/baseline')), ('candidate', ROOT)]:
        run(label + '-' + name, [binary, 'check', '--no-cache', '--output-format', 'json', '.'],
            cwd=root, expected=(0, 1), seconds=60)
        findings[name] = ruff_findings((R / (MODE + '-' + label + '-' + name + '.log')).read_text(), root)
        if findings[name]:
            STEPS[-1]['status'] = 'LINT_FINDINGS_RECORDED'
    compare_ruff(findings['candidate'], findings['baseline'], set(M['overlay_sha256']))
    (R / (MODE + '-' + label + '-comparison.json')).write_text(json.dumps(dict(
        status='NO_NEW_OR_CHANGED_FILE_VIOLATIONS', binary=binary,
        baseline_findings=[dict(filename=k[0], code=k[1], message=k[2], count=v)
                           for k,v in findings['baseline'].items()],
        candidate_findings=[dict(filename=k[0], code=k[1], message=k[2], count=v)
                            for k,v in findings['candidate'].items()],
        changed_files_clean=True, full_repository_linted=True,
        inherited_findings_are_not_a_clean_lint_pass=bool(findings['baseline'])), indent=2))


def installed_consumer(wheel, tested, *, controls=False):
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        for name, digest in tested.items():
            if name.endswith('.py') and name.startswith(('app/', 'alembic/')):
                if name not in names:
                    raise LaneFailure('MISSING_INSTALLED_MODULE', name)
                if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                    raise LaneFailure('INSTALLED_SOURCE_HASH_MISMATCH', name)
        assert 'alembic/versions/20261107_add_skill_bundle_resources.py' in names
        assert 'alembic/versions/20261111_merge_skill_bundle_memory_search.py' in names
    run('installed-wheel', ['uv', 'pip', 'install', '--offline', '--no-deps', '--target', '/work/installed', str(wheel)], seconds=30)
    demo = Path('/work/demo')
    demo.mkdir()
    run('installed-demo', [sys.executable, '/packet/installed_demo.py'], cwd=demo,
        env=dict(ENV, PYTHONPATH='/work/installed:/packet', INSTALLED_CONSUMER_PHASE=MODE), seconds=120)
    if controls:
        run('installed-demo-wrong-version-control',
            [sys.executable, '/packet/installed_demo.py', '--wrong-version-control'],
            cwd=demo, expected=1,
            env=dict(ENV, PYTHONPATH='/work/installed:/packet', INSTALLED_CONSUMER_PHASE=MODE),
            seconds=30)
        actual = (R / (MODE + '-installed-demo-wrong-version-control.log')).read_text()
        assert 'AssertionError: INSTALLED_WHEEL_VERSION_MISMATCH' in actual
        (R / (MODE + '-installed-consumer-controls.json')).write_text(json.dumps(dict(
            status='PASS', realInstalledSevenGETPositive=True,
            wrongVersionAssertionRejected=True, beforeExpensiveFullSuites=True)))
    run('installed-console', [sys.executable, '/work/installed/bin/forgetful', '--help'], cwd=demo,
        env=dict(ENV, PYTHONPATH='/work/installed:/packet'), seconds=30)


if MODE == 'normalize':
    apt_root = Path('/opt/apt-evidence')
    assert apt_root.is_dir(), 'SIGNED_APT_EVIDENCE_MISSING'
    apt_rows = []
    for file in sorted(apt_root.rglob('*')):
        if file.is_file():
            assert not file.is_symlink()
            raw = file.read_bytes()
            assert len(raw) <= 4 * 1024**2
            name = 'apt-' + '-'.join(file.relative_to(apt_root).parts)
            with (R / name).open('xb') as stream:
                stream.write(raw)
            apt_rows.append(dict(name=name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
    assert apt_rows and sum(row['bytes'] for row in apt_rows) <= 16 * 1024**2
    (R / 'signed-apt-installed-freeze-receipt.json').write_text(json.dumps(dict(
        state='INSTALLED_SIGNED_APT_FREEZE_NOT_IMMUTABLE_HISTORICAL_PAYLOAD_PIN',
        files=apt_rows, packageIndexesRetainedAsHashes=True), indent=2))
    assert fingerprint(ROOT) == M['overlay_sha256']
    files = [name for name in M['overlay_sha256'] if name.endswith('.py')]
    before = {name: (ROOT / name).read_text() for name in files}
    import tomllib
    cfg = {}
    for name in ('ruff.toml', '.ruff.toml'):
        if (ROOT / name).is_file():
            cfg = tomllib.loads((ROOT / name).read_text())
            break
    else:
        cfg = tomllib.loads((ROOT / 'pyproject.toml').read_text()).get('tool', {}).get('ruff', {})
    lint = cfg.get('lint', {})
    enabled = {*cfg.get('select', []), *cfg.get('extend-select', []), *lint.get('select', []), *lint.get('extend-select', [])}
    chosen = [c for c in ('I', 'COM', 'Q') if 'ALL' in enabled or c in enabled]
    if chosen:
        run('style-only', ['ruff', 'check', '--fix-only', '--no-cache', '--select', ','.join(chosen), *files], seconds=60)
    assert all(normalized_ast(before[name]) == normalized_ast((ROOT / name).read_text()) for name in files)
    ruff_gate('full-source-style-preflight')
    facts = fingerprint(ROOT)
    for name in facts:
        dest = R / 'normalized-source' / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, dest)
    (R / 'tested-hashes.json').write_text(json.dumps(facts, indent=2))
    (R / 'normalize-result.json').write_text(json.dumps(dict(status='PASS', steps=STEPS, chosen=chosen)))
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


candidate = MODE.startswith('regression-green-') or MODE in ('candidate', 'postgres', 'image', 'early-sqlite', 'early-postgres', 'controller-fixture')
if MODE in ('diagnosed-sqlite', 'diagnosed-postgres'):
    candidate = True
if candidate:
    TESTED = json.loads((R / 'tested-hashes.json').read_text())
    assert fingerprint(ROOT) == TESTED
if MODE in ('candidate', 'early-sqlite'):
    # Lint the frozen source before the build creates ignored app/_version.py.
    lane('ruff-full', lambda: ruff_gate('ruff-full'))

dist = R / (MODE + '-dist' if MODE.startswith('regression-red-') else
            'baseline-dist' if MODE.startswith('baseline') else 'dist')
try:
    if not dist.exists():
        run('root-build', ['uv', 'build', '--offline', '--no-build-isolation',
                          '--python', '/buildenv/bin/python', '--out-dir', str(dist)],
            env=dict(ENV, SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0+pagination215'), seconds=120)
    wheel = next(dist.glob('*.whl'))
    run('root-install', ['uv', 'pip', 'install', '--offline', '--no-deps',
                         '--target', '/work/rootpkg', str(wheel)], seconds=30)
    run('git-preflight', ['git', '--version'], seconds=10)
    if MODE in ('baseline-postgres', 'postgres', 'image', 'early-postgres'):
        run('docker-cli-preflight', ['docker', '--version'], seconds=10)
    if MODE == 'diagnosed-postgres':
        run('docker-cli-preflight', ['docker', '--version'], seconds=10)
    run('console-preflight', ['forgetful', '--help'], seconds=30)
    run('models-before', [sys.executable, '/packet/cache_identity.py', '/models'], seconds=60)
except LaneFailure as exc:
    raise HarnessFailure('Root install/preflight failure is terminal', str(exc)) from exc

if MODE.startswith('regression-'):
    backend = MODE.rsplit('-', 1)[1]
    red = MODE.startswith('regression-red-')
    config = M['pagination_regressions'][backend]
    selection = R / (MODE + '-regression-selection.json')
    expected_ids = set(config['selected_nodeids'])
    expected_failures = set(config['baseline_assertion_failures']) if red else set()
    assert expected_ids and (not red or expected_failures <= expected_ids and expected_failures)
    # Run actual pytest; exit1 alone never constitutes a successful red proof.
    try:
        run('regression', [sys.executable, '-m', 'pytest', '-p', 'selection_identity',
            '-p', 'no:cacheprovider', *config['selectors'], '-vv',
            '-m', 'e2e' if backend == 'postgres' else 'not e2e',
            '--basetemp=/tmp/pytest-' + MODE,
            '--junitxml=' + str(R / (MODE + '-regression.xml'))],
            env=dict(ENV, SELECTION_EVIDENCE_FILE=str(selection)),
            expected=1 if red else 0, seconds=M['pytest_timeout_seconds'][MODE])
        actual = json.loads(selection.read_text())
        classified = classify_regression(actual, expected_ids, expected_failures)
        status = 'PASS'
    except BaseException:
        (R / (MODE + '-result.json')).write_text(json.dumps(dict(
            status='FAIL', classification='REGRESSION_OR_HARNESS_NOT_PROVEN', steps=STEPS), indent=2))
        raise
    (R / (MODE + '-result.json')).write_text(json.dumps(dict(status=status,
        classification='KNOWN_ASSERTION_RED' if red else 'EXACT_REGRESSION_GREEN',
        baselineAppUnrepaired=red, selected=sorted(expected_ids),
        expectedFailures=sorted(expected_failures), steps=STEPS), indent=2))
    raise SystemExit(0)


if MODE == 'controller-fixture':
    fixture = ROOT / 'tests/_controller_fixture'
    fixture.mkdir()
    ok = fixture / 'test_success.py'
    bad = fixture / 'test_failure.py'
    ok.write_text('def test_success():\n    assert 2 + 2 == 4\n')
    bad.write_text('def test_failure():\n    assert False, "synthetic controller failure"\n')
    pytest_lane('fixture-success', [str(ok.relative_to(ROOT))])
    try:
        pytest_lane('fixture-failure', [str(bad.relative_to(ROOT))])
    except LaneFailure:
        failure = json.loads((R / (MODE + '-fixture-failure-outcome.json')).read_text())
        assert failure['actual_exitstatus'] == 1 and failure['selection_evidence_present']
    else:
        raise AssertionError('Actual pytest failure was swallowed')
    success = json.loads((R / (MODE + '-fixture-success-selection.json')).read_text())
    failure = json.loads((R / (MODE + '-fixture-failure-selection.json')).read_text())
    assert len(success['selected_nodeids']) == len(failure['selected_nodeids']) == 1
    assert success['exitstatus'] == 0 and failure['exitstatus'] == 1
    # Same actual classifier, passive plugin and pytest dependency as pagination lanes.
    cases = {
        'success': ('def test_case():\n    assert True\n', True, False),
        'assertion': ('def test_case():\n    assert False, "known assertion red"\n', True, True),
        'collection': ('def test_case(:\n    pass\n', False, True),
        'import': ('import controller_fixture_missing_module_215\ndef test_case():\n    pass\n', False, True),
        'setup': ('import pytest\n@pytest.fixture\ndef broken():\n    raise RuntimeError("setup")\ndef test_case(broken):\n    pass\n', False, True),
        'teardown': ('import pytest\n@pytest.fixture\ndef broken():\n    yield\n    raise RuntimeError("teardown")\ndef test_case(broken):\n    pass\n', False, True),
        'wrong-ids': ('def test_case():\n    assert False\n', False, True),
        'nonassert': ('def test_case():\n    raise RuntimeError("not a product assertion")\n', False, True),
    }
    classifications = {}
    fixture_end = time.monotonic() + 60
    for label, (code, accepted, red) in cases.items():
        file = fixture / ('test_classifier_' + label.replace('-', '_') + '.py')
        file.write_text(code)
        rel = str(file.relative_to(ROOT))
        selection = R / (MODE + '-classifier-' + label + '-selection.json')
        left = fixture_end - time.monotonic()
        assert left > 0, 'CLASSIFIER_FIXTURE_TOTAL_DEADLINE'
        run('classifier-' + label, [sys.executable, '-m', 'pytest', '-p', 'selection_identity',
            '-p', 'no:cacheprovider', rel, '-q', '--basetemp=/tmp/classifier-' + label],
            env=dict(ENV, SELECTION_EVIDENCE_FILE=str(selection)),
            expected=(0, 1, 2), seconds=min(20, left))
        facts = json.loads(selection.read_text())
        node = rel + ('::test_not_the_selected_case' if label == 'wrong-ids' else '::test_case')
        try:
            classify_regression(facts, {node}, {node} if red else set())
        except AssertionError as exc:
            observed = False
            reason = str(exc)
        else:
            observed = True
            reason = None
        classifications[label] = dict(expectedAccepted=accepted, actualAccepted=observed,
                                      actualPytestExit=facts['exitstatus'], refusal=reason)
        # Persist actual failure evidence before the control assertion.
        (R / 'regression-classifier-controls.json').write_text(json.dumps(classifications, indent=2))
        assert observed == accepted, 'REAL_PYTEST_CLASSIFIER_CONTROL_FAILED_' + label
    (R / (MODE + '-result.json')).write_text(json.dumps(dict(status='PASS', actual_pytest_success_and_failure=True,
        regression_classifier_real_pytest_controls=classifications, tested_hashes=TESTED, steps=STEPS), indent=2))
    raise SystemExit(0)


source_paths = ['tests/architecture', 'tests/integration', 'tests/e2e_sqlite', 'tests/e2e_provider_sessions']
native_paths = ['tests/e2e', 'tests/e2e_provider_sessions']
if MODE in ('diagnosed-sqlite', 'diagnosed-postgres'):
    backend = 'postgres' if MODE == 'diagnosed-postgres' else 'sqlite'
    expected = M['diagnosed_existing_nodeids'][backend]
    assert len(expected) == len(set(expected)) == 9
    try:
        elapsed = pytest_lane('diagnosed-existing', expected,
                              marker='e2e' if backend == 'postgres' else 'not e2e')
        facts = json.loads((R / (MODE + '-diagnosed-existing-selection.json')).read_text())
        classification = classify_regression(facts, expected, set())
        assert not facts['deselected_nodeids'] and not facts['collection_reports']
        (R / (MODE + '-green-classification.json')).write_text(json.dumps(classification, indent=2))
        if MODE == 'diagnosed-sqlite':
            from ownership_mutations import verify_ownership_mutants
            verify_ownership_mutants(ROOT, R, MODE, ENV, run, M['candidate_required_nodeids']['sqlite'], classify_regression)
        run('models-after', [sys.executable, '/packet/cache_identity.py', '/models'], seconds=60)
        assert fingerprint(ROOT) == TESTED
    except (LaneFailure, AssertionError) as exc:
        STEPS.append(dict(name='diagnosed-existing', status='FAIL', error=str(exc)))
        (R / (MODE + '-result.json')).write_text(json.dumps(dict(status='FAIL', tested_hashes=TESTED, steps=STEPS)))
        raise SystemExit(1) from exc
    (R / (MODE + '-result.json')).write_text(json.dumps(dict(status='PASS', tested_hashes=TESTED,
        tests_seconds=elapsed, steps=STEPS, existingCaseIDs=expected, classification=classification)))
    raise SystemExit(0)


if MODE in ('early-sqlite', 'early-postgres'):
    try:
        run('early-imports', [sys.executable, '-c',
            'from app.bootstrap import build_runtime; from app.repositories.sqlite.project_repository import SqliteProjectRepository; from app.repositories.postgres.project_repository import PostgresProjectRepository'], seconds=30)
        backend = 'postgres' if MODE == 'early-postgres' else 'sqlite'
        early_seconds = pytest_lane('early-owner-behavior',
            M['early_pagination_paths'][backend],
            marker='e2e' if backend == 'postgres' else 'not e2e')
        selection = json.loads((R / (MODE + '-early-owner-behavior-selection.json')).read_text())
        assert set(selection['selected_nodeids']) == set(M['candidate_required_nodeids'][backend])
        assert all(selection['reports'].get(node, {}).get('call') == 'passed'
                   for node in M['candidate_required_nodeids'][backend])
        classified_green = classify_regression(
            selection, M['candidate_required_nodeids'][backend], set())
        (R / (MODE + '-green-classification.json')).write_text(
            json.dumps(classified_green, indent=2))
    except (LaneFailure, AssertionError) as exc:
        STEPS.append(dict(name='early-owner-behavior', status='FAIL', error=str(exc)))
        (R / (MODE + '-result.json')).write_text(json.dumps(dict(status='FAIL', tested_hashes=TESTED, steps=STEPS), indent=2))
        raise SystemExit(1) from exc
    if MODE == 'early-sqlite':
        installed_consumer(wheel, TESTED, controls=True)
    run('models-after', [sys.executable, '/packet/cache_identity.py', '/models'], seconds=60)
    assert fingerprint(ROOT) == TESTED
    status = 'FAIL' if any(step['status'] == 'FAIL' for step in STEPS) else 'PASS'
    (R / (MODE + '-result.json')).write_text(json.dumps(dict(status=status, tested_hashes=TESTED, tests_seconds=early_seconds, steps=STEPS), indent=2))
    raise SystemExit(0 if status == 'PASS' else 1)

if MODE in ('baseline', 'baseline-postgres'):
    if MODE == 'baseline-postgres':
        run('pg-client', ['pg_dump', '--version'], seconds=10)
    elapsed = pytest_lane('full', native_paths if MODE.endswith('postgres') else source_paths,
                          marker='e2e' if MODE.endswith('postgres') else 'not e2e')
    (R / (MODE + '-result.json')).write_text(json.dumps(dict(status='CALIBRATION_COMPLETE', tests_seconds=elapsed, steps=STEPS)))
else:
    assert MODE in ('candidate', 'postgres', 'image', 'early-sqlite', 'early-postgres')
    if MODE == 'candidate':
        lane('full-source', lambda: pytest_lane('full', source_paths))
        lane('installed', lambda: installed_consumer(wheel, TESTED))
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
