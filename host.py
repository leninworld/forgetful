"""One bounded owned public GitHub VM attempt; never run on the Mac.

Host code only orchestrates Docker and verifies public bytes. Project execution,
dependency installation, builds, models and tests all occur in Linux containers.
"""
from pathlib import Path
import base64
import hashlib
import json
import os
import platform
import re
import selectors
import shutil
import signal
import sys
import subprocess
import time
import urllib.parse
import urllib.request

P = Path(__file__).resolve().parent
SOURCE = P.parent / 'source'
R = P.parent / 'runtime'
M = json.loads((P / 'manifest.json').read_text())
START = float(os.environ['VALIDATION_JOB_STARTED_EPOCH'])
END = START + M['target_seconds']
RUN = os.environ['GITHUB_RUN_ID']
assert re.fullmatch('[0-9]+', RUN)
OWN = []
ACTIVE = None
DOCKER_ROOT = None
OWNED_VM_VERIFIED = False
LOGS = []
SAMPLES = []
LAST_SAMPLE = 0
MISSED_SAMPLES = 0
PHASE = 'setup'
RESULT = {'status': 'PREPARING', 'full_validation': False, 'job_start': START,
          'target': END, 'absolute': START + M['absolute_seconds'],
          'native_windows': 'NOT_RUN', 'upstream_ci_equivalence': False}
routes = [line.split()[0] for line in Path('/proc/net/route').read_text().splitlines()[1:]
          if line.split()[1] == '00000000']
assert len(set(routes)) == 1
RX = Path('/sys/class/net') / routes[0] / 'statistics/rx_bytes'
NETWORK_START = int(RX.read_text())


def memory():
    return int(re.search(r'^MemAvailable:\s+(\d+)', Path('/proc/meminfo').read_text(), re.M)[1]) * 1024


def guard():
    assert time.time() < END, 'TARGET_DEADLINE_NO_RETRY'
    assert memory() >= 1536 * 1024**2, 'HOST_MEMORY_PRESSURE'
    for destination in (P, DOCKER_ROOT):
        if destination is not None:
            assert shutil.disk_usage(destination).free >= 8 * 1024**3, 'HOST_OR_DOCKER_DISK_PRESSURE'
            fs = os.statvfs(destination)
            assert not fs.f_files or fs.f_favail / fs.f_files >= .05, 'HOST_INODE_PRESSURE'
    assert int(RX.read_text()) - NETWORK_START < M['network_bytes_cap'], 'NETWORK_CAP'
    evidence = sum(f.stat().st_size for f in R.glob('*') if f.is_file())
    assert evidence < M['output_bytes_cap'], 'EVIDENCE_OUTPUT_CAP'


class StepFailure(Exception):
    pass


def command(label, args, seconds=30, cap=4 * 1024**2, monitor=True, expected=0):
    global LAST_SAMPLE, MISSED_SAMPLES
    guard()
    assert re.fullmatch('[a-z0-9-]+', label)
    target = R / (label + '.log')
    assert not target.exists()
    # Explicitly exclude every Actions/token variable from subprocesses/containers.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('ACTIONS_') and not any(x in k.upper()
           for x in ('TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL', 'API_KEY'))}
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True, env=env)
    sel = selectors.DefaultSelector()
    sel.register(process.stdout, selectors.EVENT_READ)
    data = bytearray()
    begin = time.monotonic()
    failure = None
    try:
        while sel.get_map():
            guard()
            assert time.monotonic() - begin < seconds, 'STEP_TIMEOUT_' + label
            if monitor and ACTIVE and time.monotonic() - begin >= 3 and time.monotonic() - LAST_SAMPLE >= 5:
                LAST_SAMPLE = time.monotonic()
                probe = (
                    "import json,os,pathlib; p=pathlib.Path('/sys/fs/cgroup'); "
                    "v=dict(line.split() for line in (p/'memory.stat').read_text().splitlines()); "
                    "c=int((p/'memory.current').read_text()); f=int(v.get('inactive_file',0)); "
                    "print(json.dumps({'current':c,'inactive_file':f,'working_set':max(0,c-f),"
                    "'peak':int((p/'memory.peak').read_text()),'limit':int((p/'memory.max').read_text()),"
                    "'mounts':{d:{'free':os.statvfs(d).f_bavail*os.statvfs(d).f_frsize,"
                    "'inodes':os.statvfs(d).f_files,'available_inodes':os.statvfs(d).f_favail} "
                    "for d in ['/tmp','/work','/runtime']}}))"
                )
                try:
                    observed = subprocess.run(['docker', 'exec', ACTIVE, '/opt/app/.venv/bin/python', '-c', probe],
                                              capture_output=True, text=True, timeout=8)
                    if observed.returncode != 0:
                        raise RuntimeError('monitor exit ' + str(observed.returncode))
                    row = json.loads(observed.stdout)
                except (subprocess.TimeoutExpired, RuntimeError, ValueError) as missed:
                    MISSED_SAMPLES += 1
                    SAMPLES.append(dict(phase=PHASE, at=time.time(), missed_sample=True,
                                        consecutive_misses=MISSED_SAMPLES, error_type=type(missed).__name__))
                    assert MISSED_SAMPLES < 3, 'THREE_CONSECUTIVE_MONITOR_MISSES'
                else:
                    MISSED_SAMPLES = 0
                    row.update(phase=PHASE, at=time.time(), host_available=memory())
                    row['raw_cgroup_peak_bytes'] = row['peak']
                    SAMPLES.append(row)
                    assert row['working_set'] < .9375 * row['limit'], 'APP_WORKING_SET_PRESSURE'
                    for mount in row['mounts'].values():
                        assert mount['free'] >= 1024**3, 'INNER_DISK_PRESSURE'
                        assert not mount['inodes'] or mount['available_inodes'] / mount['inodes'] >= .05, 'INNER_INODE_PRESSURE'
            for key, _ in sel.select(.25):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                assert len(data) + len(chunk) <= cap, 'STEP_OUTPUT_CAP_' + label
                data.extend(chunk)
        actual = process.wait(timeout=3)
        if actual != expected:
            raise StepFailure(label, actual)
        return bytes(data)
    except BaseException as exc:
        failure = str(exc)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        raise
    finally:
        sel.close()
        target.write_bytes(data)
        LOGS.append({'label': label, 'exit': process.returncode, 'bytes': len(data),
                     'elapsed': time.monotonic() - begin, 'failure': failure})


def download(url, target, size, sha256=None, git_blob=None):
    allowed = {'files.pythonhosted.org', 'huggingface.co', 'openaipublic.blob.core.windows.net'}

    def permitted(address):
        u = urllib.parse.urlsplit(address)
        return u.scheme == 'https' and (u.hostname in allowed or any(
            (u.hostname or '').endswith(s) for s in ('.huggingface.co', '.hf.co', '.xethub.hf.co')))

    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, newurl):
            assert permitted(newurl), 'UNAPPROVED_REDIRECT'
            return super().redirect_request(request, response, code, message, headers, newurl)

    assert permitted(url) and not target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    begin = time.monotonic()
    count = 0
    sha = hashlib.sha256()
    git = hashlib.sha1(b'blob ' + str(size).encode() + b'\0')
    with urllib.request.build_opener(Redirect()).open(url, timeout=25) as response, target.open('xb') as out:
        assert response.status == 200
        while True:
            guard()
            assert time.monotonic() - begin < 180, 'DOWNLOAD_TIME_CAP'
            chunk = response.read(65536)
            if not chunk:
                break
            assert count + len(chunk) <= size, 'DOWNLOAD_SIZE_CAP'
            out.write(chunk)
            count += len(chunk)
            sha.update(chunk)
            git.update(chunk)
    assert count == size and (sha256 is None or sha.hexdigest() == sha256)
    assert git_blob is None or git.hexdigest() == git_blob


def model_metadata(label):
    # Bounded public metadata, before and after the unchanged online image preparation.
    for i, model in enumerate(json.loads((P / 'model-pins.json').read_text())['models']):
        url = 'https://huggingface.co/api/models/' + model['repo_id']
        with urllib.request.urlopen(url, timeout=25) as response:
            data = response.read(2097153)
        assert len(data) <= 2097152
        actual = json.loads(data)
        assert actual['sha'] == model['revision'], 'MODEL_MAIN_MOVED_NO_FLOATING_VALIDATION'
        assert not actual.get('private') and not actual.get('gated')
        (R / (label + '-model-' + str(i) + '.json')).write_bytes(data)


def phase(name, *, pg=False, docker_api=False):
    global ACTIVE, PHASE, MISSED_SAMPLES
    PHASE = name
    MISSED_SAMPLES = 0
    source = R / ('baseline' if name.startswith('baseline') or name == 'regression' else 'candidate')
    scratch = R / 'scratch' / name
    for child in ('tmp', 'work'):
        (scratch / child).mkdir(parents=True)
    args = ['docker', 'create', '--name', 'forgetful193-' + RUN + '-' + name,
            '--label', 'org.forgetful193.run=' + RUN, '--init', '--read-only',
            '--user', str(os.getuid()) + ':' + str(os.getgid()),
            '--memory', '1g' if name == 'image' else '8g',
            '--memory-swap', '1g' if name == 'image' else '8g', '--cpus', '3',
            '--pids-limit', '512', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--network', 'container:forgetful-db' if pg else 'none',
            '--mount', 'type=bind,src=' + str(scratch / 'tmp') + ',dst=/tmp',
            '--mount', 'type=bind,src=' + str(scratch / 'work') + ',dst=/work',
            '--mount', 'type=bind,src=' + str(P) + ',dst=/packet,readonly',
            '--mount', 'type=bind,src=' + str(R) + ',dst=/runtime',
            '--mount', 'type=bind,src=' + str(source) + ',dst=/source,readonly',
            '--mount', 'type=bind,src=' + str(R / 'models') + ',dst=/models,readonly',
            '--mount', 'type=bind,src=' + str(R / 'tokenizer') + ',dst=/tokenizer,readonly']
    if docker_api:
        # Root-equivalent API on this isolated disposable VM, not a read-only socket.
        # Exact review must cover native fixture inspection AND owned image tests.
        args += ['--group-add', str(Path('/var/run/docker.sock').stat().st_gid),
                 '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
                 '--mount', 'type=bind,src=' + DOCKER + ',dst=/usr/local/bin/docker,readonly']
    values = {'CI': 'true', 'DATABASE': 'SQLite', 'SQLITE_MEMORY': 'true',
              'HOME': '/work/home', 'XDG_CONFIG_HOME': '/work/config', 'XDG_DATA_HOME': '/work/data',
              'FASTEMBED_CACHE_DIR': '/models', 'FASTEMBED_LOCAL_FILES_ONLY': 'true',
              'HF_HUB_OFFLINE': '1', 'TIKTOKEN_CACHE_DIR': '/tokenizer', 'DO_NOT_TRACK': '1',
              'PYTHONDONTWRITEBYTECODE': '1', 'UV_OFFLINE': '1', 'UV_CACHE_DIR': '/tmp/uv-cache',
              'HOST_PACKET_PATH': str(P), 'VALIDATION_RUN_ID': RUN,
              'FORGETFUL_TEST_IMAGE': 'forgetful193-shipping:' + RUN,
              'FORGETFUL_TEST_VERSION': '0.0.0+skill193',
              'VALIDATION_PHASE_DEADLINE_EPOCH': str(END - 20)}
    if pg:
        values.update(POSTGRES_HOST='127.0.0.1', PGPORT='5099', POSTGRES_USER='forgetful',
                      POSTGRES_PASSWORD='forgetful', POSTGRES_DB='forgetful', SESSION_TEST_POSTGRES_PORT='5099', EXPECTED_POSTGRES_ID=PG)
    for key, value in values.items():
        args += ['--env', key + '=' + value]
    args += ['forgetful193-tools:' + RUN, '/opt/app/.venv/bin/python', '/packet/inside.py', name]
    cid = command(name + '-create', args, 20).decode().strip()
    assert re.fullmatch('[a-f0-9]{64}', cid)
    OWN.append(cid)
    ACTIVE = cid
    failed = None
    try:
        try:
            command(name + '-run', ['docker', 'start', '--attach', cid], min(1800, END - time.time() - 20))
        except StepFailure as exc:
            failed = exc
        state = json.loads(command(name + '-state', ['docker', 'inspect', '--format', '{{json .State}}', cid],
                                   10, 8192, False))
        assert not state['Running'] and not state['OOMKilled']
        # Results are already on the live bind mount, not copied after a tmpfs disappears.
        result_file = R / (name + '-result.json')
        if not result_file.is_file():
            raise RuntimeError('SETUP_OR_HARNESS_FAILURE_TERMINAL_' + name)
        facts = json.loads(result_file.read_text())
        resources = json.loads((R / (name + '-resources-after.json')).read_text())
        assert resources['peak'] <= resources['limit']
        for mount in resources['mounts'].values():
            assert mount['free'] >= 1024**3
            assert not mount['inodes'] or mount['available_inodes'] / mount['inodes'] >= .05
        facts['resources'] = resources
        if failed is not None and facts['status'] != 'FAIL':
            raise failed
        return facts
    finally:
        ACTIVE = None


def provision_postgres():
    data = R / 'pgdata'
    data.mkdir(mode=0o777)
    pg = command('pg-create', ['docker', 'create', '--name', 'forgetful-db',
        '--label', 'org.forgetful193.run=' + RUN, '--network', 'none', '--memory', '1g',
        '--memory-swap', '1g', '--cpus', '1', '--pids-limit', '128',
        '--mount', 'type=bind,src=' + str(data) + ',dst=/var/lib/postgresql/data',
        '--health-cmd', 'pg_isready -U forgetful -d forgetful -p 5099',
        '--health-interval', '2s', '--health-timeout', '2s', '--health-retries', '20',
        '-e', 'POSTGRES_USER=forgetful', '-e', 'POSTGRES_PASSWORD=forgetful', '-e', 'POSTGRES_DB=forgetful',
        images['pgvector16'], '-p', '5099']).decode().strip()
    OWN.append(pg)
    command('pg-start', ['docker', 'start', pg])
    for index in range(30):
        status = command('pg-health-' + str(index), ['docker', 'inspect', '--format', '{{.State.Health.Status}}', pg]).decode().strip()
        if status == 'healthy':
            return pg
        time.sleep(2)
    raise RuntimeError('POSTGRES_SETUP_FAILURE_TERMINAL')


def build(label, recipe, context, tag, *, base=None):
    args = ['docker', 'build', '--memory', '4g', '--memory-swap', '4g',
            '--cpu-period', '100000', '--cpu-quota', '300000', '--file', str(recipe), '--tag', tag]
    if base is not None:
        args += ['--build-arg', 'PYTHON_BASE=' + base]
    else:
        args += ['--build-arg', 'VERSION=0.0.0+skill193']
    command(label, [*args, str(context)], 600)


try:
    assert platform.system() == 'Linux' and platform.machine() == 'x86_64'
    assert os.environ.get('GITHUB_ACTIONS') == 'true'
    assert os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
    assert os.environ.get('GITHUB_REPOSITORY') == 'leninworld/forgetful'
    assert 0 <= time.time() - START < 300
    assert M['runtime_approved'] and M['docker_api_approved'] and M['diagnostic_branch_approved']
    assert M['style_normalization_approved'], 'EXPLICIT_PREFREEZE_STYLE_ADJUDICATION_REQUIRED'
    R.mkdir()
    for folder in ('inputs', 'models', 'tokenizer', 'scratch'):
        (R / folder).mkdir()
    DOCKER = shutil.which('docker')
    assert DOCKER is not None
    assert command('initial-containers', [DOCKER, 'ps', '--quiet']) == b'', 'VM_NOT_EXCLUSIVE'
    OWNED_VM_VERIFIED = True
    RESULT['docker_cli'] = dict(path=DOCKER, sha256=hashlib.sha256(Path(DOCKER).read_bytes()).hexdigest(),
                               version=command('docker-version', [DOCKER, '--version']).decode())
    info = json.loads(command('docker-info', [DOCKER, 'info', '--format',
        '{"root":{{json .DockerRootDir}},"memory":{{.MemTotal}},"cpus":{{.NCPU}},"cgroup":{{json .CgroupVersion}}}']))
    DOCKER_ROOT = info['root']
    assert info['cgroup'] == '2'
    assert info['cpus'] >= 4 and memory() >= 12 * 1024**3
    assert all(shutil.disk_usage(path).free >= 40 * 1024**3 for path in (P, DOCKER_ROOT))
    RESULT['admission'] = dict(memory_available=memory(), workspace_free=shutil.disk_usage(P).free,
                               docker_free=shutil.disk_usage(DOCKER_ROOT).free, docker=info)
    assert command('source-clean', ['git', '-C', str(SOURCE), 'status', '--porcelain']) == b''
    assert command('source-base', ['git', '-C', str(SOURCE), 'rev-parse', 'HEAD']).decode().strip() == M['base']
    for folder in (SOURCE, P):
        credentials = subprocess.run(['git', '-C', str(folder), 'config', '--local', '--get-regexp',
                                      'extraheader|credential'], capture_output=True, timeout=5)
        assert credentials.returncode == 1 and credentials.stdout == b'', 'PERSISTED_GIT_CREDENTIALS'
    for name, digest in M['packet_sha256'].items():
        assert hashlib.sha256((P / name).read_bytes()).hexdigest() == digest
    for name, digest in M['source_sha256'].items():
        assert hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() == digest
    for name in M['new_source_paths']:
        assert not (SOURCE / name).exists(), 'NEW_PATH_COLLISION'
    for name in ('baseline', 'candidate'):
        shutil.copytree(SOURCE, R / name, ignore=shutil.ignore_patterns('.git'))
    for name, digest in M['overlay_sha256'].items():
        raw = (P / 'overlay' / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        dest = R / 'candidate' / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
    # Exercise the changed evidence path offline before model/dependency downloads or full tests.
    images = {x['name']: x['repository'].replace('library/', '') + '@' + x['digest'] for x in M['images']}
    command('pull-python313', ['docker', 'pull', '--platform', 'linux/amd64', images['python313']], 300)
    tiny_id = command('tiny-create', ['docker', 'create', '--name', 'forgetful193-tiny-' + RUN,
        '--label', 'org.forgetful193.run=' + RUN, '--network', 'none', '--memory', '128m',
        '--memory-swap', '128m', '--cpus', '1', '--pids-limit', '64',
        '-v', str(P) + ':/packet:ro', '-v', str(SOURCE) + ':/source:ro', images['python313'], 'python', '/packet/tiny_regression_fixture.py']).decode().strip()
    assert re.fullmatch('[0-9a-f]{64}', tiny_id)
    OWN.append(tiny_id)
    command('tiny-run', ['docker', 'start', '-a', tiny_id], 30)
    tiny_state = json.loads(command('tiny-state', ['docker', 'inspect', '--format', '{{json .State}}', tiny_id]))
    assert tiny_state['ExitCode'] == 0 and not tiny_state['OOMKilled'] and not tiny_state['Running']
    for item in json.loads((P / 'artifacts.json').read_text()):
        dest = R / ('tokenizer' if item['name'].startswith('9b5ad') else 'inputs') / item['name']
        download(item['url'], dest, item['bytes'], sha256=item['sha256'])
    for model in json.loads((P / 'model-pins.json').read_text())['models']:
        repo = R / 'models' / ('models--' + model['repo_id'].replace('/', '--'))
        for item in model['required_files']:
            dest = repo / 'snapshots' / model['revision'] / item['rfilename']
            url = 'https://huggingface.co/' + model['repo_id'] + '/resolve/' + model['revision'] + '/' + item['rfilename']
            download(url, dest, item['size'], sha256=item.get('lfs', {}).get('sha256'),
                     git_blob=None if 'lfs' in item else item['blobId'])
        (repo / 'refs').mkdir(parents=True)
        (repo / 'refs/main').write_text(model['revision'])
    images = {x['name']: x['repository'].replace('library/', '') + '@' + x['digest'] for x in M['images']}
    for name, image in images.items():
        if name == 'python313':
            continue
        command('pull-' + name, ['docker', 'pull', '--platform', 'linux/amd64', image], 300)
    # One supported resource-bound builder route; never fall back to an unbounded builder.
    os.environ['DOCKER_BUILDKIT'] = '0'
    help_text = command('bounded-build-help', ['docker', 'build', '--help']).decode()
    assert '--memory' in help_text and '--memory-swap' in help_text and '--cpu-quota' in help_text
    context = R / 'build-context'
    context.mkdir()
    (context / 'packet').mkdir()
    for name in ('pgdg-key.asc', 'build-constraints.txt'):
        shutil.copyfile(P / name, context / 'packet' / name)
    shutil.copytree(R / 'inputs', context / 'inputs')
    for name in ('pyproject.toml', 'uv.lock'):
        shutil.copyfile(R / 'candidate' / name, context / name)
    build('build-tools', P / 'Dockerfile', context, 'forgetful193-tools:' + RUN, base=images['python313'])
    assert time.time() - START < 12 * 60, 'COLD_SETUP_BUDGET'
    phase('normalize')
    tested = json.loads((R / 'tested-hashes.json').read_text())
    for name, digest in tested.items():
        raw = (R / 'normalized-source' / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        (R / 'candidate' / name).write_bytes(raw)
    # All candidate hashes are now fixed BEFORE either original baseline begins.
    PG = provision_postgres()
    assert phase('docker-preflight', pg=True, docker_api=True)['status'] == 'PASS'
    baseline = phase('baseline')
    native_baseline = phase('baseline-postgres', pg=True, docker_api=True)
    assert baseline['status'] == native_baseline['status'] == 'CALIBRATION_COMPLETE'
    peaks = [row['working_set'] for row in SAMPLES if row['phase'] in ('baseline', 'baseline-postgres') and 'working_set' in row]
    peaks.extend([baseline['resources']['working_set'], native_baseline['resources']['working_set']])
    assert all(any(row['phase'] == name and 'working_set' in row for row in SAMPLES) for name in ('baseline', 'baseline-postgres'))
    assert len(peaks) >= 2, 'INSUFFICIENT_BASELINE_MEMORY_SAMPLES'
    projected_memory = max(4 * 1024**3, 2 * max(peaks) + 1024**3)
    assert projected_memory <= 8 * 1024**3, 'FULL_FINISH_MEMORY_NO_GO'
    projected_seconds = (1.35 * (baseline['tests_seconds'] + native_baseline['tests_seconds']) + 480
                         + M['shipping_build_reserve_seconds'] + M['image_reserve_seconds'] + M['export_reserve_seconds'])
    assert time.time() + projected_seconds < END, 'FULL_FINISH_TIME_NO_GO'
    RESULT['calibration'] = dict(baseline=baseline, native_baseline=native_baseline,
                                full_sampled_working_set_peak=max(peaks),
                                raw_cgroup_peaks=[baseline['resources']['peak'], native_baseline['resources']['peak']],
                                projected_memory=projected_memory,
                                projected_seconds=projected_seconds)
    assert phase('regression')['status'] == 'PASS'
    candidate = phase('candidate')
    native = phase('postgres', pg=True, docker_api=True)
    command('pg-stop', ['docker', 'stop', '--time', '5', PG])
    recipe = (R / 'candidate/docker/Dockerfile').read_text()
    assert recipe.count('FROM python:3.12-slim') == 1
    assert recipe.count('COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv') == 1
    recipe = recipe.replace('FROM python:3.12-slim', 'FROM ' + images['python312'])
    recipe = recipe.replace('COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv',
        'COPY --from=forgetful193-tools:' + RUN + ' /usr/local/bin/uv /usr/local/bin/uv\n'
        'COPY --from=forgetful193-tools:' + RUN + ' /opt/build-constraints.txt /opt/build-constraints.txt\n'
        'ENV UV_BUILD_CONSTRAINT=/opt/build-constraints.txt UV_HTTP_RETRIES=0')
    frozen_recipe = R / 'Dockerfile.shipping-pinned'
    frozen_recipe.write_text(recipe)
    build('build-shipping', frozen_recipe, R / 'candidate', 'forgetful193-shipping:' + RUN)
    model_metadata('before-shipping')
    image = phase('image', docker_api=True)
    model_metadata('after-shipping')
    assert all(row['status'] == 'PASS' for row in (candidate, native, image)), 'CANDIDATE_FAILURE_NO_AUTOMATIC_CORRECTION'
    assert all(hashlib.sha256((R / 'candidate' / name).read_bytes()).hexdigest() == digest for name, digest in tested.items())
    RESULT.update(status='FULL_VALIDATION_PASS', full_validation=True, tested_hashes=tested,
                  label='hosted Docker', hosted_api_scope='owned native PG inspection and owned shipping-image tests',
                  style_changes='Explicit pre-baseline I/COM/Q normalization with AST/import-set guard; exact diff retained',
                  shipping_recipe_substitutions='immutable official base, verified uv and isolated build constraints; exact recipe retained')
except BaseException as exc:
    RESULT.update(status='STOPPED_NO_RETRY', error=str(exc), full_validation=False)
    raise
finally:
    # Stop only owned containers on this verified ephemeral VM. No remove/prune/host-file cleanup.
    stopped = False
    cleanup_error = None
    try:
        # A fresh github-hosted job with verified zero initial running containers
        # owns its newly created build/test containers, including unlabeled build steps.
        query = ['docker', 'ps', '--quiet'] if OWNED_VM_VERIFIED else [
            'docker', 'ps', '--quiet', '--filter', 'label=org.forgetful193.run=' + RUN]
        children = subprocess.run(query, capture_output=True, text=True, timeout=10)
        owned = children.stdout.split() if children.returncode == 0 else []
        stopped = children.returncode == 0
        for cid in dict.fromkeys([*reversed(OWN), *owned]):
            outcome = subprocess.run(['docker', 'stop', '--time', '5', cid], capture_output=True, timeout=15)
            stopped = stopped and outcome.returncode == 0
        remaining = subprocess.run(query, capture_output=True, text=True, timeout=10)
        stopped = stopped and remaining.returncode == 0 and not remaining.stdout.strip()
    except BaseException as cleanup:
        cleanup_error = str(cleanup)
        stopped = False
    if not stopped and RESULT['full_validation']:
        RESULT.update(status='OWNED_STOP_UNVERIFIED', full_validation=False)
    RESULT['cleanup_error'] = cleanup_error
    if time.time() >= START + M['absolute_seconds']:
        RESULT.update(status='ABSOLUTE_SESSION_CEILING_REACHED', full_validation=False)
    RESULT.update(elapsed=time.time() - START, network_rx_bytes=int(RX.read_text()) - NETWORK_START,
                  logs=LOGS, samples=SAMPLES, owned_containers=OWN, owned_stop=stopped)
    if R.exists():
        (R / 'result.json').write_text(json.dumps(RESULT, indent=2))
    exports = list(R.glob('*')) if R.exists() else []
    if (R / 'normalized-source').is_dir():
        exports += list((R / 'normalized-source').rglob('*'))
    for file in sorted(exports):
        if not file.is_file():
            continue
        raw = file.read_bytes()
        data = re.sub(rb'(https://[^\s<>"\x27?]+)\?[^\s<>"\x27]+', rb'\1?REDACTED', raw)
        name = str(file.relative_to(R))
        print('EVIDENCE_BEGIN ' + name + ' ' + str(len(data)) + ' ' + hashlib.sha256(data).hexdigest(), flush=True)
        for index in range(0, len(data), 16384):
            print('EVIDENCE_CHUNK ' + name + ' ' + str(index // 16384) + ' ' + base64.b64encode(data[index:index + 16384]).decode(), flush=True)
        print('EVIDENCE_END ' + name, flush=True)



    if sys.exc_info()[0] is None and not RESULT['full_validation']:
        raise SystemExit(1)
