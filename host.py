"""One bounded owned public GitHub VM attempt; never run on the Mac.

Host code only orchestrates Docker and verifies public bytes. Project execution,
dependency installation, builds, models and tests all occur in Linux containers.
"""
from pathlib import Path
import base64
import hashlib
import json
import math
import os
import platform
import re
import selectors
import shutil
import signal
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
LOGS = []
SAMPLES = []
LAST_SAMPLE = 0
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
    assert shutil.disk_usage(P).free >= 4 * 1024**3, 'HOST_DISK_PRESSURE'
    assert int(RX.read_text()) - NETWORK_START < M['network_bytes_cap'], 'NETWORK_CAP'
    evidence = sum(f.stat().st_size for f in R.glob('*') if f.is_file())
    assert evidence < M['output_bytes_cap'], 'EVIDENCE_OUTPUT_CAP'


def command(label, args, seconds=30, cap=4 * 1024**2, monitor=True, expected=0):
    global LAST_SAMPLE
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
            if monitor and ACTIVE and time.monotonic() - LAST_SAMPLE >= 5:
                LAST_SAMPLE = time.monotonic()
                # The percentage is a sampled working set, not a guaranteed future peak.
                raw = command('sample-' + str(len(LOGS)),
                    ['docker', 'stats', '--no-stream', '--format', '{{.MemPerc}}', ACTIVE],
                    8, 8192, False).decode().strip()
                assert re.fullmatch(r'[0-9]+(?:\.[0-9]+)?%', raw)
                percent = float(raw[:-1])
                SAMPLES.append({'phase': PHASE, 'at': time.time(), 'percent': percent,
                                'upper_bytes': math.ceil((percent + .01) / 100 * 8 * 1024**3) + 1024**2})
                assert percent < 93.75, 'APP_WORKING_SET_PRESSURE'
            for key, _ in sel.select(.25):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                assert len(data) + len(chunk) <= cap, 'STEP_OUTPUT_CAP_' + label
                data.extend(chunk)
        assert process.wait(timeout=3) == expected, 'STEP_FAILED_' + label
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
    global ACTIVE, PHASE
    PHASE = name
    source = R / ('baseline' if name.startswith('baseline') else 'candidate')
    args = ['docker', 'create', '--name', 'forgetful170-' + RUN + '-' + name,
            '--label', 'org.forgetful170.run=' + RUN, '--init', '--read-only',
            '--user', str(os.getuid()) + ':' + str(os.getgid()),
            '--memory', '1g' if name == 'image' else '8g',
            '--memory-swap', '1g' if name == 'image' else '8g', '--cpus', '3',
            '--pids-limit', '512', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--network', 'container:forgetful-db' if pg else 'none',
            '--tmpfs', '/tmp:rw,exec,size=128m,mode=1777',
            '--tmpfs', '/work:rw,exec,size=1g,mode=1777',
            '--mount', 'type=bind,src=' + str(P) + ',dst=/packet,readonly',
            '--mount', 'type=bind,src=' + str(R) + ',dst=/runtime',
            '--mount', 'type=bind,src=' + str(source) + ',dst=/source',
            '--mount', 'type=bind,src=' + str(R / 'models') + ',dst=/models,readonly',
            '--mount', 'type=bind,src=' + str(R / 'tokenizer') + ',dst=/tokenizer,readonly']
    if docker_api:
        # This grants root-equivalent access ONLY to this owned ephemeral public VM.
        # A read-only bind of the socket would NOT limit API writes.
        args += ['--group-add', str(Path('/var/run/docker.sock').stat().st_gid),
                 '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
                 '--mount', 'type=bind,src=' + DOCKER + ',dst=/usr/local/bin/docker,readonly']
    values = {'CI': 'true', 'DATABASE': 'SQLite', 'SQLITE_MEMORY': 'true',
              'HOME': '/work/home', 'XDG_CONFIG_HOME': '/work/config', 'XDG_DATA_HOME': '/work/data',
              'FASTEMBED_CACHE_DIR': '/models', 'FASTEMBED_LOCAL_FILES_ONLY': 'true',
              'HF_HUB_OFFLINE': '1', 'TIKTOKEN_CACHE_DIR': '/tokenizer', 'DO_NOT_TRACK': '1',
              'PYTHONDONTWRITEBYTECODE': '1', 'UV_OFFLINE': '1', 'UV_CACHE_DIR': '/tmp/uv-cache',
              'HOST_PACKET_PATH': str(P), 'VALIDATION_RUN_ID': RUN,
              'FORGETFUL_TEST_IMAGE': 'forgetful170-shipping:' + RUN,
              'FORGETFUL_TEST_VERSION': '0.0.0+skill170'}
    if pg:
        values.update(POSTGRES_HOST='127.0.0.1', PGPORT='5099', POSTGRES_USER='forgetful',
                      POSTGRES_PASSWORD='forgetful', POSTGRES_DB='forgetful',
                      SESSION_TEST_POSTGRES_PORT='5099', FORGETFUL_TEST_PLUGIN_PG_HOST='127.0.0.1',
                      FORGETFUL_TEST_PLUGIN_PG_PORT='5099')
    for key, value in values.items():
        assert not key.startswith('ACTIONS_') and key != 'GITHUB_TOKEN'
        args += ['--env', key + '=' + value]
    args += ['forgetful170-tools:' + RUN, '/opt/app/.venv/bin/python', '/packet/inside.py', name]
    cid = command(name + '-create', args, 20).decode().strip()
    assert re.fullmatch('[a-f0-9]{64}', cid)
    OWN.append(cid)
    ACTIVE = cid
    try:
        command(name + '-run', ['docker', 'start', '--attach', cid], min(1800, END - time.time() - 20))
        state = json.loads(command(name + '-state', ['docker', 'inspect', '--format', '{{json .State}}', cid],
                                   10, 8192, False))
        assert not state['Running'] and not state['OOMKilled'] and state['ExitCode'] == 0
    finally:
        ACTIVE = None


def provision_postgres():
    pg = command('pg-create', ['docker', 'create', '--name', 'forgetful-db',
        '--label', 'org.forgetful170.run=' + RUN, '--network', 'none', '--memory', '1g',
        '--memory-swap', '1g', '--cpus', '1', '--pids-limit', '128',
        '--tmpfs', '/var/lib/postgresql/data:rw,exec,size=512m',
        '--health-cmd', 'pg_isready -U forgetful -d forgetful -p 5099',
        '--health-interval', '2s', '--health-timeout', '2s', '--health-retries', '20',
        '-e', 'POSTGRES_USER=forgetful', '-e', 'POSTGRES_PASSWORD=forgetful', '-e', 'POSTGRES_DB=forgetful',
        images['pgvector16'], '-p', '5099']).decode().strip()
    OWN.append(pg)
    command('pg-start', ['docker', 'start', pg])
    for i in range(30):
        status = command('pg-health-' + str(i), ['docker', 'inspect', '--format', '{{.State.Health.Status}}', pg]).decode().strip()
        if status == 'healthy':
            break
        time.sleep(2)
    assert status == 'healthy'
    for i, sql in enumerate(["CREATE USER forgetful_skill_plugin_test WITH SUPERUSER PASSWORD 'synthetic-test-password'",
                              'CREATE DATABASE forgetful_skill_plugin_test OWNER forgetful_skill_plugin_test']):
        command('pg-role-' + str(i), ['docker', 'exec', pg, 'psql', '-p', '5099', '-U', 'forgetful',
                                     '-d', 'forgetful', '-v', 'ON_ERROR_STOP=1', '-c', sql])
    return pg


try:
    assert platform.machine() == 'x86_64' and os.environ.get('GITHUB_ACTIONS') == 'true'
    assert os.environ.get('GITHUB_REPOSITORY') == 'leninworld/forgetful'
    assert 0 <= time.time() - START < 300
    assert M['runtime_approved'] and M['docker_api_approved'] and M['publication_approved']
    R.mkdir()
    for folder in ['inputs', 'models', 'tokenizer']:
        (R / folder).mkdir()
    DOCKER = shutil.which('docker')
    assert DOCKER is not None
    RESULT['docker_cli'] = {'path': DOCKER, 'sha256': hashlib.sha256(Path(DOCKER).read_bytes()).hexdigest(),
                            'version': command('docker-version', [DOCKER, '--version']).decode()}
    info = json.loads(command('docker-info', [DOCKER, 'info', '--format',
        '{"root":{{json .DockerRootDir}},"memory":{{.MemTotal}},"cpus":{{.NCPU}}}']))
    assert info['cpus'] >= 4 and memory() >= 12 * 1024**3
    assert shutil.disk_usage(P).free >= 20 * 1024**3
    assert shutil.disk_usage(info['root']).free >= 20 * 1024**3
    RESULT['admission'] = {'memory_available': memory(), 'workspace_free': shutil.disk_usage(P).free,
                            'docker_free': shutil.disk_usage(info['root']).free, 'docker': info}
    assert command('source-clean', ['git', '-C', str(SOURCE), 'status', '--porcelain']) == b''
    assert command('source-base', ['git', '-C', str(SOURCE), 'rev-parse', 'HEAD']).decode().strip() == M['base']
    for folder in (SOURCE, P):
        result = subprocess.run(['git', '-C', str(folder), 'config', '--local', '--get-regexp',
                                 'extraheader|credential'], capture_output=True, timeout=5)
        assert result.returncode == 1 and result.stdout == b'', 'PERSISTED_GIT_CREDENTIALS'
    for name, sha in M['source_sha256'].items():
        assert hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() == sha
    for name in ('baseline', 'candidate'):
        shutil.copytree(SOURCE, R / name, ignore=shutil.ignore_patterns('.git'))
    for name, sha in M['overlay_sha256'].items():
        assert hashlib.sha256((P / 'overlay' / name).read_bytes()).hexdigest() == sha
        target = R / 'candidate' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(P / 'overlay' / name, target)
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
        command('pull-' + name, ['docker', 'pull', '--platform', 'linux/amd64', image], 300)
    context = R / 'build-context'
    context.mkdir()
    for name in ['pyproject.toml', 'uv.lock']:
        shutil.copyfile(R / 'candidate' / name, context / name)
    command('build-tools', ['docker', 'build', '--file', str(P / 'Dockerfile'),
        '--build-context', 'packet=' + str(P), '--build-context', 'verified_inputs=' + str(R / 'inputs'),
        '--build-arg', 'PYTHON_BASE=' + images['python313'], '--tag', 'forgetful170-tools:' + RUN,
        str(context)], 600)
    assert time.time() - START < 12 * 60, 'COLD_SETUP_BUDGET'
    phase('lint')
    tested = json.loads((R / 'tested-hashes.json').read_text())
    for name, sha in tested.items():
        data = (R / 'tested-source' / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha
        (R / 'candidate' / name).write_bytes(data)
    phase('baseline')
    pg = provision_postgres()
    phase('baseline-postgres', pg=True, docker_api=True)
    baseline = json.loads((R / 'baseline-result.json').read_text())
    samples = [x['upper_bytes'] for x in SAMPLES if x['phase'] in ('baseline', 'baseline-postgres')]
    assert len(samples) >= 2, 'INSUFFICIENT_BASELINE_MEMORY_SAMPLES'
    peak = max(samples)
    projected_memory = max(4 * 1024**3, 2 * peak + 1024**3)
    assert projected_memory <= 8 * 1024**3, 'FULL_FINISH_MEMORY_NO_GO'
    postgres_baseline = json.loads((R / 'baseline-postgres-result.json').read_text())
    assert baseline['status'] == postgres_baseline['status'] == 'CALIBRATION_COMPLETE'
    projected_seconds = 1.35 * (baseline['tests_seconds'] + postgres_baseline['tests_seconds']) + 480
    assert time.time() + projected_seconds < END, 'FULL_FINISH_TIME_NO_GO'
    RESULT['calibration'] = {'baseline': baseline, 'postgres_baseline': postgres_baseline, 'sampled_peak_upper': peak,
                             'projected_memory': projected_memory, 'projected_seconds': projected_seconds,
                             'assumptions': '2x max of both full baseline sampled memories +1GiB, minimum4GiB; 1.35x measured source+Postgres suites +480s targeted/build/installed/shipping; assumptions not guaranteed bounds'}
    phase('candidate')
    phase('postgres', pg=True, docker_api=True)
    command('pg-stop', ['docker', 'stop', '--time', '5', pg])
    recipe = (R / 'candidate/docker/Dockerfile').read_text()
    assert recipe.count('FROM python:3.12-slim') == 1
    assert recipe.count('COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv') == 1
    recipe = recipe.replace('FROM python:3.12-slim', 'FROM ' + images['python312'])
    recipe = recipe.replace('COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv',
        'COPY --from=forgetful170-tools:' + RUN + ' /usr/local/bin/uv /usr/local/bin/uv\n'
        'COPY --from=forgetful170-tools:' + RUN + ' /opt/build-constraints.txt /opt/build-constraints.txt\n'
        'ENV UV_BUILD_CONSTRAINT=/opt/build-constraints.txt UV_HTTP_RETRIES=0')
    frozen_recipe = R / 'Dockerfile.shipping-pinned'
    frozen_recipe.write_text(recipe)
    command('build-shipping', ['docker', 'build', '--file', str(frozen_recipe),
        '--build-arg', 'VERSION=0.0.0+skill170', '--tag', 'forgetful170-shipping:' + RUN,
        str(R / 'candidate')], 600)
    model_metadata('before-shipping')
    phase('image', docker_api=True)
    model_metadata('after-shipping')
    for name in ('candidate', 'postgres', 'image'):
        assert json.loads((R / (name + '-result.json')).read_text())['status'] == 'PASS'
    assert all(hashlib.sha256((R / 'candidate' / n).read_bytes()).hexdigest() == h for n, h in tested.items())
    RESULT.update(status='FULL_VALIDATION_PASS', full_validation=True, tested_hashes=tested,
                  shipping_recipe_substitutions='immutable Python base, same uv0.9.30 binary, exact isolated build constraints/no automatic HTTP retries; application recipe unchanged')
except BaseException as exc:
    RESULT.update(status='STOPPED_NO_RETRY', error=str(exc), full_validation=False)
    raise
finally:
    # Only this fresh ephemeral hosted VM, never Mac/external Docker data.
    children = subprocess.run(['docker', 'ps', '--quiet', '--filter',
                               'label=org.forgetful170.run=' + RUN],
                              capture_output=True, text=True, timeout=10)
    children = children.stdout.split() if children.returncode == 0 else []
    stopped = True
    for cid in dict.fromkeys([*reversed(OWN), *children]):
        result = subprocess.run(['docker', 'stop', '--time', '5', cid], capture_output=True, timeout=15)
        stopped = stopped and result.returncode == 0
    remaining = subprocess.run(['docker', 'ps', '--quiet', '--filter',
                                'label=org.forgetful170.run=' + RUN], capture_output=True, timeout=10)
    stopped = stopped and remaining.returncode == 0 and not remaining.stdout.strip()
    # Fixture normally removes its own UUID names/volumes. Retain any failure leftovers
    # on the disposable VM for evidence; no broad prune or prefix-based delete.
    RESULT.update(elapsed=time.time() - START, network_rx_bytes=int(RX.read_text()) - NETWORK_START,
                  logs=LOGS, samples=SAMPLES, owned_containers=OWN, owned_stop=stopped)
    if R.exists():
        (R / 'result.json').write_text(json.dumps(RESULT, indent=2))
    for file in sorted(R.iterdir()) if R.exists() else []:
        if file.is_file() and file.suffix in ('.log', '.xml', '.json', '.diff'):
            # Retain raw evidence only inside this disposable VM. Public log output
            # removes signed download query strings; never claim it is byte-identical.
            raw = file.read_bytes()
            data = re.sub(rb'(https://[^\s<>"\x27?]+)\?[^\s<>"\x27]+', rb'\1?REDACTED', raw)
            print('EVIDENCE_BEGIN ' + file.name + ' ' + str(len(data)) + ' '
                  + hashlib.sha256(data).hexdigest(), flush=True)
            for index in range(0, len(data), 16384):
                print('EVIDENCE_CHUNK ' + file.name + ' ' + str(index // 16384) + ' '
                      + base64.b64encode(data[index:index + 16384]).decode(), flush=True)
            print('EVIDENCE_END ' + file.name, flush=True)
