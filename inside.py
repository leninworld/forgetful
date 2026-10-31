"""All project runtime executes in the owned Linux validation container."""
from pathlib import Path
import difflib
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile

P = Path('/packet')
R = Path('/runtime')
M = json.loads((P / 'manifest.json').read_text())
MODE = sys.argv[1]
ROOT = Path('/source')
os.chdir(ROOT)
ENV = dict(os.environ, PYTHONPATH=str(ROOT) + ':/work/rootpkg:/packet',
           PATH='/work/rootpkg/bin:' + os.environ['PATH'], PYTHONDONTWRITEBYTECODE='1')
assert platform.python_version().startswith('3.13.')
assert platform.machine() == 'x86_64'
assert hashlib.sha256((ROOT / 'uv.lock').read_bytes()).hexdigest() == M['lock_sha256']


def run(name, args, *, expected=0, cwd=ROOT, env=ENV, seconds=900):
    target = R / (MODE + '-' + name + '.log')
    assert not target.exists()
    begin = time.monotonic()
    with target.open('x') as log:
        result = subprocess.run(args, cwd=cwd, env=env, stdout=log,
                                stderr=subprocess.STDOUT, timeout=seconds)
    print(json.dumps({'phase': MODE, 'step': name, 'exit': result.returncode,
                      'elapsed': time.monotonic() - begin}), flush=True)
    assert result.returncode == expected, name + '\n' + target.read_text()[-12000:]
    return time.monotonic() - begin


def pytest(name, paths, marker='not e2e', extra=()):
    evidence = R / (MODE + '-' + name + '-selection.json')
    elapsed = run(name, [sys.executable, '-m', 'pytest', '-p', 'selection_identity',
                         *paths, '-m', marker, '-q',
                         '--junitxml=' + str(R / (MODE + '-' + name + '.xml')), *extra],
                  env=dict(ENV, SELECTION_EVIDENCE_FILE=str(evidence)))
    facts = json.loads(evidence.read_text())
    assert facts['exitstatus'] == 0
    for path in paths:
        rows = [row for file, row in facts['files'].items()
                if file == path or file.startswith(path.rstrip('/') + '/')]
        assert sum(row['executed'] for row in rows) > 0, 'EMPTY_OR_ENTIRELY_SKIPPED_PATH: ' + path
    if MODE == 'candidate' and name == 'full':
        baseline = json.loads((R / 'baseline-full-original-selection.json').read_text())
        for file, old in baseline['files'].items():
            if file not in M['overlay_sha256']:
                assert facts['files'].get(file, {}).get('executed', 0) >= old['executed'], file
    return elapsed


if MODE == 'lint':
    assert all(hashlib.sha256((ROOT / n).read_bytes()).hexdigest() == h
               for n, h in M['overlay_sha256'].items())
    changed = [n for n in M['overlay_sha256'] if n.endswith('.py')]
    run('ruff-fix', ['ruff', 'check', '--fix', '--no-cache', *changed], seconds=60)
    run('ruff-full', ['ruff', 'check', '--no-cache', '.'], seconds=60)
    hashes = {}
    delta = []
    for name in M['overlay_sha256']:
        data = (ROOT / name).read_bytes()
        hashes[name] = hashlib.sha256(data).hexdigest()
        copy = R / 'tested-source' / name
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(data)
        delta.extend(difflib.unified_diff((P / 'overlay' / name).read_text().splitlines(True),
                                          data.decode().splitlines(True),
                                          fromfile='a/' + name, tofile='b/' + name))
    (R / 'ruff-fix.diff').write_text(''.join(delta))
    (R / 'tested-hashes.json').write_text(json.dumps(hashes, indent=2))
    print('LINT_FULL_PASS', flush=True)
    raise SystemExit(0)

# Faithfully install the real root package and its console scripts before any
# baseline or candidate workload. Dependencies remain in the frozen image; only
# the root wheel is installed into a fresh writable job-local target.
# Both baseline phases reuse the same exact baseline build, not a passing test
# result. Candidate packaging artifacts are reused by its installed API/CLI lane.
dist = R / ('baseline-dist' if MODE.startswith('baseline') else 'dist')
if not dist.exists():
    run('root-build', ['uv', 'build', '--offline', '--no-build-isolation',
                      '--python', '/buildenv/bin/python', '--out-dir', str(dist)],
        env=dict(ENV, SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0+skill170'), seconds=120)
wheel = next(dist.glob('*.whl'))
run('root-install', ['uv', 'pip', 'install', '--offline', '--no-deps',
                     '--python', '/opt/app/.venv/bin/python',
                     '--target', '/work/rootpkg', str(wheel)], seconds=30)
run('git-preflight', ['git', '--version'], seconds=10)
run('real-cli-preflight', ['forgetful', '--help'], seconds=30)
run('models', [sys.executable, '/packet/cache_identity.py', '/models'])
run('distribution-identity', [sys.executable, '-c',
    'import importlib.metadata as m,json,platform; '
    'print(json.dumps({"python":platform.python_version(),'
    '"packages":{d.metadata["Name"]:d.version for d in m.distributions() if d.metadata["Name"]}}))'])
if MODE == 'baseline':
    run('public-startup-oracle', [sys.executable, '/packet/startup_oracle.py'], expected=42, seconds=90)
    assert 'MISSING_FEATURE_ADMIN_REGISTRATION_SILENTLY_IGNORED' in (
        R / 'baseline-public-startup-oracle.log').read_text()
    elapsed = pytest('full-original', ['tests/architecture', 'tests/integration',
                                      'tests/e2e_sqlite', 'tests/e2e_provider_sessions'])
    (R / 'baseline-result.json').write_text(json.dumps({
        'status': 'CALIBRATION_COMPLETE', 'tests_seconds': elapsed,
        'oracle': 'real startup ignored configured invalid registration; expected42'}))
    raise SystemExit(0)

if MODE == 'baseline-postgres':
    run('pg-client-version', ['pg_dump', '--version'], seconds=10)
    elapsed = pytest('full-native-original', ['tests/e2e', 'tests/e2e_provider_sessions'], marker='e2e')
    (R / 'baseline-postgres-result.json').write_text(json.dumps({
        'status': 'CALIBRATION_COMPLETE', 'tests_seconds': elapsed}))
    raise SystemExit(0)

assert MODE in ('candidate', 'postgres', 'image')
TESTED = json.loads((R / 'tested-hashes.json').read_text())
assert all(hashlib.sha256((ROOT / n).read_bytes()).hexdigest() == h for n, h in TESTED.items())
if MODE == 'candidate':
    run('public-startup-oracle', [sys.executable, '/packet/startup_oracle.py'], seconds=90)
    targeted = ['tests/integration/test_skill_write_plugin.py',
                'tests/integration/test_skill_plugin_runtime.py',
                'tests/integration/test_skill_plugin_transports.py',
                'tests/integration/test_skill_service.py']
    pytest('targeted', targeted)
    pytest('full', ['tests/architecture', 'tests/integration', 'tests/e2e_sqlite',
                    'tests/e2e_provider_sessions'])
    document = (ROOT / 'docs/configuration.md').read_text().split('## Skill Plugin\n', 1)[1]
    example = re.search(r'```python\n(.*?)```', document, re.S).group(1)
    example_path = ROOT / 'synthetic_admin_example.py'
    example_path.write_text(example)
    run('documented-example', [sys.executable, str(example_path)], seconds=30)
    # These exact post-Ruff candidate artifacts were built for root installation
    # above. Reuse them; do not resolve/build/install all dependencies again.
    wheel = next((R / 'dist').glob('*.whl'))
    sdist = next((R / 'dist').glob('*.tar.gz'))
    with zipfile.ZipFile(wheel) as archive:
        assert any('/versions/' in n and 'alembic' in n for n in archive.namelist())
        for name in ['skill_plugin_protocol.py', 'skill_plugin_service.py', 'skill_plugin_models.py']:
            assert any(n.endswith('/' + name) for n in archive.namelist())
    with tarfile.open(sdist) as archive:
        assert any(n.endswith('/LICENCE.md') for n in archive.getnames())
    # Install root wheel without resolving dependencies; use the already frozen app environment.
    run('wheel-install', ['uv', 'pip', 'install', '--offline', '--no-deps',
                         '--target', '/work/installed', str(wheel)], seconds=30)
    demo = Path('/work/demo')
    demo.mkdir()
    # The preserved smoke script expects /work/installed and a separate working directory.
    env = dict(ENV, PYTHONPATH='/work/installed')
    run('installed-api', [sys.executable, '/packet/installed_smoke.py'], cwd=demo, env=env, seconds=90)
    run('installed-cli', [sys.executable, '/work/installed/bin/forgetful', '--help'],
        cwd=demo, env=env, seconds=30)
    example_path.unlink()  # Only synthetic job-internal file, never a Mac/external path.
elif MODE == 'postgres':
    run('pg-client-version', ['pg_dump', '--version'], seconds=10)
    pytest('full-native', ['tests/e2e', 'tests/e2e_provider_sessions',
                           'tests/integration/test_skill_write_plugin.py'], marker='e2e')
else:
    pytest('shipping-offline', ['tests/docker'], extra=('-p', 'image_identity_plugin'))
run('models-after', [sys.executable, '/packet/cache_identity.py', '/models'])
assert all(hashlib.sha256((ROOT / n).read_bytes()).hexdigest() == h for n, h in TESTED.items())
(R / (MODE + '-result.json')).write_text(json.dumps({'status': 'PASS', 'tested_hashes': TESTED}))
