"""Observe the image fixture after real preparation and before teardown.

Every command reaches the original helper and actual preinstalled Docker CLI.
The sole argument augmentation bounds real child runs to 6 GiB/3 CPU/512 PIDs
and labels their ownership. No response, health check, image or transport is
fabricated. Two extra read-only inspections check the real shipping cache.
"""
import os
from pathlib import Path
import subprocess

import pytest

active = None


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    if item.module.__name__.endswith('test_offline_image'):
        original = item.module.docker
        if getattr(original, '_resource_policy', False):
            return

        def bounded(*args, **kwargs):
            if args and args[0] == 'run':
                args = (args[0], '--memory', '6g', '--memory-swap', '6g', '--cpus', '3',
                        '--pids-limit', '512', '--label',
                        'org.forgetful172.run=' + os.environ['VALIDATION_RUN_ID'], *args[1:])
            return original(*args, **kwargs)

        bounded._resource_policy = True
        item.module.docker = bounded


def verify(value, phase):
    image, name, cache, _, _ = value
    assert name.startswith('forgetful-offline-') and cache == name + '-cache'
    packet = os.environ['HOST_PACKET_PATH']
    command = ['docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
               '--name', name + '-identity-' + phase,
               '--label', 'org.forgetful172.run=' + os.environ['VALIDATION_RUN_ID'],
               '--memory', '512m', '--memory-swap', '512m', '--cpus', '1',
               '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--mount', 'type=volume,src=' + cache + ',dst=/cache,readonly',
               '--mount', 'type=bind,src=' + packet + ',dst=/packet,readonly',
               '--entrypoint', '/app/.venv/bin/python', image,
               '/packet/cache_identity.py', '/cache/fastembed']
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    Path('/runtime/image-cache-' + phase + '.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    global active
    outcome = yield
    if fixturedef.argname == 'offline_image' and outcome.excinfo is None:
        active = outcome.get_result()
        verify(active, 'prepared')


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    if active is not None and 'offline_image' in item.fixturenames:
        verify(active, 'after-use')
