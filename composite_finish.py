"""Exact215 composite reference/bridge/consumer; stdlib only, no project imports."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def checked_inputs(root, expected):
    root = Path(root)
    assert {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()} == set(expected), 'I3_INPUT_SET'
    for name, value in expected.items():
        path = root / name
        assert not path.is_symlink() and path.is_file(), 'I3_INPUT_MISSING'
        assert digest(path) == value, 'I3_INPUT_HASH'
    return json.loads((root / 'reference.json').read_text())


def imports(packet, runtime, manifest):
    source = packet / 'prior-I3'
    reference = checked_inputs(source, manifest['composite_prior_inputs'])
    assert reference['origin']['run'] == 43901923810 and reference['origin']['full_validation'] is False
    target = runtime / 'imported-I3'
    shutil.copytree(source, target)
    checked_inputs(target, manifest['composite_prior_inputs'])
    # Existing inside.py reads these names. They are reference aliases, not new receipts.
    for name in ('baseline-full-selection.json', 'baseline-postgres-full-selection.json'):
        (runtime / name).symlink_to(Path('imported-I3') / name)
    save(runtime / 'composite-reference-origins.json', {
        'origin': reference['origin'], 'files': manifest['composite_prior_inputs'],
        'referenceAliasesNotCurrentRunReceipts': ['baseline-full-selection.json', 'baseline-postgres-full-selection.json']})
    return reference


def packages(text):
    result = {}
    for line in text.splitlines():
        assert re.fullmatch(r'[A-Za-z0-9_.-]+==[^\s=]+', line), 'FREEZE_FORMAT'
        name, version = line.split('==')
        name = re.sub('[-_.]+', '-', name).lower()
        assert name not in result, 'FREEZE_DUPLICATE'
        result[name] = version
    return result


def check_versions(app, build, reference):
    for name, text, count in [('app', app, 122), ('build', build, 10)]:
        actual = packages(text)
        expected = {re.sub('[-_.]+', '-', k).lower(): v for k, v in reference['packages'][name].items()}
        assert len(actual) == len(expected) == count and actual == expected, 'PACKAGE_VERSION_SET_' + name


def tuples(text):
    rows = [tuple(line.split('\t')) for line in text.splitlines()]
    assert rows and all(len(row) == 3 and all(row) for row in rows), 'APT_TUPLES'
    assert len(rows) == len(set(rows)), 'APT_DUPLICATE'
    return set(rows)


def checked_identity_split(tested, prior, overrides):
    extra = 'tests/plan_external_ref_cases.py'
    assert len(prior) == 45 and extra not in prior, 'I3_MAP_NOT_45'
    assert set(overrides) == {'inside.py'}, 'PIN_OVERRIDE_KEYS'
    assert re.fullmatch('[0-9a-f]{64}', overrides['inside.py']), 'PIN_OVERRIDE_HASH'
    assert set(tested) == set(prior) | {extra} and len(tested) == 46, 'EXACT_46_KEYS'
    assert {name: tested[name] for name in prior} == prior, 'ORIGINAL_45_CHANGED'
    assert re.fullmatch('[0-9a-f]{64}', tested[extra]), 'EXTRA_NORMALIZED_HASH'
    return {name: tested[name] for name in prior}


def bridge(packet, runtime):
    manifest = json.loads((packet / 'manifest.json').read_text())
    reference = checked_inputs(runtime / 'imported-I3', manifest['composite_prior_inputs'])
    overrides = manifest['composite_pin_overrides']
    assert set(overrides) == {'inside.py'}, 'PIN_OVERRIDE_KEYS'
    history = manifest['composite_pin_override_origins']
    assert set(history) == {'inside.py'}
    assert history['inside.py']['I3SHA256'] == reference['pinFiles']['inside.py']
    assert history['inside.py']['approvedSHA256'] == overrides['inside.py']
    for name, expected in reference['pinFiles'].items():
        assert digest(packet / name) == overrides.get(name, expected), 'COMPOSITE_PIN_CHANGED_' + name
    for name, expected in reference['pins'].items():
        assert manifest[name] == expected, 'COMPOSITE_MANIFEST_PIN_CHANGED'
    assert sys.version_info[:3] == (3, 13, 18), 'CPYTHON_BRIDGE'
    actual = {}
    for name in ('app', 'build'):
        path = Path('/opt') / (name + '-freeze.txt')
        raw = path.read_bytes()
        assert len(raw) <= 65536, 'FREEZE_CAP'
        (runtime / ('actual-' + name + '-freeze.txt')).write_bytes(raw)
        actual[name] = raw.decode()
    check_versions(actual['app'], actual['build'], reference)
    apt_files = []
    for stage in ('bootstrap', 'pgdg'):
        actual = Path('/opt/apt-evidence') / stage / 'installed.tsv'
        prior = runtime / 'imported-I3' / ('apt-' + stage + '-installed.tsv')
        assert tuples(actual.read_text()) == tuples(prior.read_text()), 'APT_VERSION_SET_' + stage
        apt_files.append({'stage': stage, 'actualSHA256': digest(actual), 'priorSHA256': digest(prior)})
    # Signed acquisition and package tuples are also exported by the unchanged normalize phase.
    checked_identity_split(json.loads((runtime / 'tested-hashes.json').read_text()),
        json.loads((runtime / 'imported-I3/tested-hashes.json').read_text()), overrides)
    save(runtime / 'composite-environment-bridge.json', {
        'status': 'VERSION_AND_PIN_BRIDGE_PASS', 'CPython': sys.version, 'appPackages': 122,
        'buildPackages': 10, 'apt': apt_files, 'imageByteIdentityClaimed': False,
        'APTQualification': 'Version/architecture tuple equality only; historical payload bytes unavailable',
        'fullToolsImageProof': 'tools-image-identity.json', 'original45NormalizedEqual': True, 'additionalTestOnlyNormalized': 'tests/plan_external_ref_cases.py',
        'totalNormalizedPaths': 46, 'insidePinOverride': history['inside.py']})


def skips(path):
    return sorted((node.attrib.get('classname'), node.attrib.get('name'),
                   node.find('skipped').attrib.get('type'), node.find('skipped').attrib.get('message'))
                  for node in ET.parse(path).getroot().iter('testcase') if node.find('skipped') is not None)


def compare_selection(current, prior, required):
    assert current['exitstatus'] == 0 and not current['collection_reports'], 'FULL_COLLECTION_FAILURE'
    assert Counter(current['selected_nodeids']) == Counter(prior['selected_nodeids'] + required), 'FULL_SELECTED_UNION'
    assert Counter(current['deselected_nodeids']) == Counter(prior['deselected_nodeids']), 'DESELECTION_CHANGED'
    for name in prior['selected_nodeids']:
        # Preserve setup/call/teardown outcome classes, including the original skips.
        assert current['reports'][name] == prior['reports'][name], 'BASELINE_OUTCOME_CHANGED'
    for name in required:
        assert current['reports'][name] == {'setup': 'passed', 'call': 'passed', 'teardown': 'passed'}, 'REPEAT_NOT_PASS'


def verify_installed_module_map(modules, original_hashes):
    """Same pure verifier for final evidence and historical-map D0 controls."""
    assert isinstance(original_hashes, dict) and len(original_hashes) == 45, 'INSTALLED_ORIGINAL_MAP'
    assert isinstance(modules, dict) and len(modules) == 29, 'INSTALLED_MODULE_COUNT'
    for row in modules.values():
        assert row['path'] in original_hashes and original_hashes[row['path']] == row['sha256'], 'INSTALLED_SOURCE_IDENTITY'
    return True


def validate_complete(runtime, manifest):
    runtime = Path(runtime)
    reference = checked_inputs(runtime / 'imported-I3', manifest['composite_prior_inputs'])
    assert json.loads((runtime / 'composite-environment-bridge.json').read_text())['status'] == 'VERSION_AND_PIN_BRIDGE_PASS'
    image = json.loads((runtime / 'tools-image-identity.json').read_text())
    assert re.fullmatch('sha256:[0-9a-f]{64}', image['Id']) and image['Architecture'] == 'amd64' and image['Os'] == 'linux'
    tested = json.loads((runtime / 'tested-hashes.json').read_text())
    original_hashes = checked_identity_split(tested, json.loads(
        (runtime / 'imported-I3/tested-hashes.json').read_text()), manifest['composite_pin_overrides'])
    for name, expected in tested.items():
        assert digest(runtime / 'normalized-source' / name) == expected, 'EXPORTED_NORMALIZED_SOURCE'
    for mode, baseline_name, backend in [('candidate', 'baseline', 'sqlite'), ('postgres', 'baseline-postgres', 'postgres')]:
        facts = json.loads((runtime / (mode + '-result.json')).read_text())
        assert facts['status'] == 'PASS' and facts['tested_hashes'] == tested, 'FRESH_CANDIDATE_FAILURE'
        current = json.loads((runtime / (mode + '-full-selection.json')).read_text())
        prior = json.loads((runtime / 'imported-I3' / (baseline_name + '-full-selection.json')).read_text())
        compare_selection(current, prior, manifest['candidate_required_nodeids'][backend])
        assert skips(runtime / (mode + '-full.xml')) == skips(runtime / 'imported-I3' / (baseline_name + '-full.xml')), 'SKIP_REASON_CHANGED'
    for mode, backend in [('diagnosed-sqlite', 'sqlite'), ('diagnosed-postgres', 'postgres')]:
        diagnosed = json.loads((runtime / (mode + '-result.json')).read_text())
        facts = json.loads((runtime / (mode + '-diagnosed-existing-selection.json')).read_text())
        expected = manifest['diagnosed_existing_nodeids'][backend]
        assert diagnosed['status'] == 'PASS' and diagnosed['tested_hashes'] == tested
        assert facts['exitstatus'] == 0 and not facts['collection_reports'] and not facts['deselected_nodeids']
        assert Counter(facts['selected_nodeids']) == Counter(expected) and len(expected) == 2
        assert set(facts['reports']) == set(expected)
        assert all(facts['reports'][name] == {'setup': 'passed', 'call': 'passed', 'teardown': 'passed'} for name in expected)
    installed = json.loads((runtime / 'candidate-installed-pagination-demo.json').read_text())
    assert installed['status'] == 'PASS' and len(installed['seven_resources']) == 7
    assert all(row['returned'] == 1 and row['filteredTotal'] == 3 and row['limit'] == 1 and row['offset'] == 1
               for row in installed['seven_resources'].values()), 'LATE_INSTALLED_GETS'
    verify_installed_module_map(installed['installed_candidate_modules'], original_hashes)
    return {'status': 'COMPOSITE_CANDIDATE_EVIDENCE_VERIFIED', 'candidateFreshRequiredCases': 218,
            'historicalOrigin': reference['origin'], 'qualifications': reference['qualifications'],
            'shippingQualification': reference['shippingQualification'], 'APTByteEqualityClaimed': False}


def validate_result(runtime, manifest, expected_run):
    runtime = Path(runtime)
    result = json.loads((runtime / 'result.json').read_text())
    if not result.get('full_validation'):
        return {'status': 'TERMINAL_NOT_A_VALIDATION_PASS', 'actualStatus': result.get('status'),
                'error': result.get('error'), 'full_validation': False, 'ownedStop': result.get('owned_stop')}
    assert result['status'] == 'COMPOSITE_FULL_VALIDATION_PASS' and result['owned_stop'] is True
    assert result['cleanup_error'] is None and result['elapsed'] < manifest['absolute_seconds']
    assert (result['attempt_number'], result['consumed_validation_attempts_before'], result['total_validation_attempt_limit']) == (5, 4, 5)
    assert result['origins']['candidateRun'] == str(expected_run), 'CURRENT_RUN_BINDING'
    assert result['origins']['I3']['run'] == 43901923810 and result['origins']['I3']['full_validation'] is False
    assert result['origins']['I4Diagnostic']['run'] == 43912489266 and result['origins']['I4Diagnostic']['full_validation'] is False
    verified = validate_complete(runtime, manifest)
    return dict(verified, status='COMPOSITE_FULL_VALIDATION_PASS', full_validation=True,
                currentRun=str(expected_run), historicalRun=43901923810, disclosureRequired=True)


def controls(output):
    results = {}
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / 'reference.json').write_text('{"synthetic": true}')
        expected = {'reference.json': digest(root / 'reference.json')}
        def test(name, accepted, function):
            failure = None
            try:
                function()
                actual = True
            except (AssertionError, FileNotFoundError, KeyError, ValueError) as error:
                actual = False
                failure = type(error).__name__ + ':' + str(error)
            results[name] = {'expectedAccepted': accepted, 'accepted': actual, 'failure': failure}
            assert actual is accepted, 'COMPOSITE_CONTROL_' + name
        test('real-input-hash-reader-positive', True, lambda: checked_inputs(root, expected))
        test('real-input-hash-reader-changed', False, lambda: checked_inputs(root, {'reference.json': '0' * 64}))
        test('real-input-hash-reader-missing', False, lambda: checked_inputs(root, {'missing.json': '0' * 64}))
        ref = {'packages': {'app': {f'a{i}': '1' for i in range(122)}, 'build': {f'b{i}': '1' for i in range(10)}}}
        text = lambda d: '\n'.join(k + '==' + v for k, v in d.items())
        app, build = text(ref['packages']['app']), text(ref['packages']['build'])
        test('exact-122-10-versions', True, lambda: check_versions(app, build, ref))
        test('extra-package', False, lambda: check_versions(app + '\nextra==1', build, ref))
        test('changed-version', False, lambda: check_versions(app.replace('a0==1', 'a0==2'), build, ref))
        prior = {'selected_nodeids': ['old', 'skipped'], 'deselected_nodeids': ['filtered'], 'reports': {
            'old': {'setup': 'passed', 'call': 'passed', 'teardown': 'passed'},
            'skipped': {'setup': 'skipped', 'teardown': 'passed'}}}
        current = json.loads(json.dumps(prior));current.update(exitstatus=0, collection_reports=[])
        current['selected_nodeids'].append('new');current['reports']['new'] = prior['reports']['old'].copy()
        test('same-path-complete-union', True, lambda: compare_selection(current, prior, ['new']))
        changed = json.loads(json.dumps(current));changed['reports']['old']['call'] = 'skipped'
        test('new-skip-refused', False, lambda: compare_selection(changed, prior, ['new']))
        changed = json.loads(json.dumps(current));changed['deselected_nodeids'].append('extra')
        test('new-deselection-refused', False, lambda: compare_selection(changed, prior, ['new']))
        changed = json.loads(json.dumps(current));changed['reports']['new']['call'] = 'failed'
        test('required-repeat-failure', False, lambda: compare_selection(changed, prior, ['new']))
    prior = {f'original-{i}': hashlib.sha256(str(i).encode()).hexdigest() for i in range(45)}
    mixed = dict(prior, **{'tests/plan_external_ref_cases.py': '1' * 64})
    override = {'inside.py': '2' * 64}
    test('46-split-positive', True, lambda: checked_identity_split(mixed, prior, override))
    changed = dict(mixed); changed['original-0'] = '3' * 64
    test('46-original-changed', False, lambda: checked_identity_split(changed, prior, override))
    added = dict(mixed); added['extra47'] = '4' * 64
    test('46-extra47', False, lambda: checked_identity_split(added, prior, override))
    test('46-missing-test', False, lambda: checked_identity_split(prior, prior, override))
    test('46-forbidden-pin', False, lambda: checked_identity_split(mixed, prior, {'other.py': '2' * 64}))
    packet = Path(__file__).resolve().parent
    control_data = json.loads((packet / 'installed-module-control-data.json').read_text())
    original_hashes = json.loads((packet / 'prior-I3/tested-hashes.json').read_text())
    assert control_data['state'] == 'HISTORICAL_I4_PRODUCTION_MAP_CONTROL_DATA_NOT_CURRENT_PASS'
    assert control_data['actualSource45MapSHA256'] == digest(packet / 'prior-I3/tested-hashes.json')
    actual_modules = control_data['installed_candidate_modules']
    test('actual-I4-installed29-source45', True, lambda: verify_installed_module_map(actual_modules, original_hashes))
    changed_modules = json.loads(json.dumps(actual_modules))
    changed_modules[next(iter(changed_modules))]['sha256'] = '0' * 64
    test('installed-module-hash-mismatch', False, lambda: verify_installed_module_map(changed_modules, original_hashes))
    test('installed-original-map-shadow-string', False, lambda: verify_installed_module_map(actual_modules, 'baseline-postgres'))
    save(output, {'status': 'PASS', 'syntheticOnly': False, 'scaffoldAndHistoricalPublicControlData': True, 'caseCount': len(results), 'cases': results,
                  'realSharedInputParserAndSelectionConsumer': True, 'projectTestsClaimed': False, 'historicalI4InstalledMapControlData': True})


if __name__ == '__main__':
    assert not sys.flags.optimize
    if sys.argv[1] == 'controls':
        controls(Path(sys.argv[2]))
    else:
        assert sys.argv[1] == 'bridge'
        bridge(Path('/packet'), Path('/runtime'))
