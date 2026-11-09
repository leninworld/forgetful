"""Real installed-wheel Task policy demonstration on a fresh native SQLite DB."""
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

import app
from app.bootstrap import build_runtime, dispose_runtime
from app.config.settings import settings
from app.exceptions import TaskPluginRejectedError
from app.models.plan_models import PlanCreate, TaskCreate, TaskUpdate
from app.models.project_models import ProjectCreate
from app.models.user_models import UserCreate
from app.routes.plan_plugin_responses import TaskTransitionResponse

assert Path(app.__file__).resolve().is_relative_to('/work/installed')
assert Path.cwd() == Path('/work/demo')
sys.path.insert(0, str(Path.cwd()))
Path('installed_task_policy.py').write_text('''
from app.models.write_plugin_models import WriteDecision, PluginWarning
from app.protocols.task_plugin_protocol import TaskPlugin
async def before(request):
    if request.submitted.title == "Blocked installed update":
        return WriteDecision(outcome="reject", message="Synthetic installed policy", code="INSTALLED_REJECT")
    return WriteDecision(outcome="warn", message="Synthetic installed warning", code="INSTALLED_WARN")
async def after(request):
    return PluginWarning(message="Synthetic saved advisory", code="INSTALLED_AFTER")
plugin = TaskPlugin(before_save={"create": before, "update": before}, after_save={"create": after})
''')
settings.PLANNING_ENABLED = True
settings.ACTIVITY_ENABLED = False
settings.RERANKING_ENABLED = False
settings.FORGETFUL_SERVER = ''
settings.FORGETFUL_SCOPES = '*'
settings.DATABASE = 'SQLite'
settings.SQLITE_MEMORY = False
settings.SQLITE_PATH = '/work/demo/task-policy.db'
settings.MEMORY_WRITE_PLUGIN = ''
settings.ENTITY_WRITE_PLUGIN = ''
settings.PLAN_WRITE_PLUGIN = ''
settings.TASK_WRITE_PLUGIN = 'installed_task_policy:plugin'
settings.TASK_WRITE_PLUGIN_MODE = 'enforce'
assert not Path(settings.SQLITE_PATH).exists()


async def main():
    runtime = await build_runtime()
    try:
        user = await runtime.services.user.get_or_create_user(UserCreate(
            external_id='installed-task-owner', name='Synthetic owner', email='task@example.test',
        ))
        project = await runtime.services.project.create_project(user.id, ProjectCreate(
            name='Installed Task demo', description='Synthetic installed SQLite policy', project_type='development',
        ))
        plan = (await runtime.services.plan.create_plan(user.id, PlanCreate(
            title='Installed plan', project_id=project.id, status='active',
        ))).plan
        result = await runtime.services.task.create_task(user.id, TaskCreate(
            title='Installed task', plan_id=plan.id,
        ))
        assert result.task is not None
        wire = TaskTransitionResponse(**result.task.model_dump(), plugin_warnings=result.plugin_warnings)
        payload = wire.model_dump(mode='json')
        assert 'task' not in payload
        assert [w['code'] for w in payload['plugin_warnings']] == ['INSTALLED_WARN', 'INSTALLED_AFTER']
        stored = await runtime.services.task.get_task(user.id, result.task.id)
        assert 'plugin_warnings' not in stored.model_dump()
        assert stored.model_dump() == result.task.model_dump()
        try:
            await runtime.services.task.update_task(user.id, result.task.id, TaskUpdate(title='Blocked installed update'))
        except TaskPluginRejectedError as exc:
            assert str(exc) == 'Synthetic installed policy'
        else:
            raise AssertionError('Installed Task policy did not reject update')
        unchanged = await runtime.services.task.get_task(user.id, result.task.id)
        assert unchanged.model_dump() == stored.model_dump()
        console_env = dict(os.environ, PYTHONPATH='/work/installed:/packet', PLANNING_ENABLED='true',
                           DATABASE='SQLite', SQLITE_MEMORY='false', SQLITE_PATH=settings.SQLITE_PATH,
                           FORGETFUL_SERVER='', FORGETFUL_SCOPES='*', TASK_WRITE_PLUGIN='')
        checked = subprocess.run(
            [sys.executable, '/work/installed/bin/forgetful', 'tools', 'info', 'create_task', '--local', '--json'],
            capture_output=True, text=True, env=console_env, timeout=30, check=False,
        )
        assert checked.returncode == 0, checked.stderr
        assert json.loads(checked.stdout)['name'] == 'create_task'
        facts = {'status': 'PASS', 'installed_origin': str(Path(app.__file__).resolve()),
                 'installed_version': importlib.metadata.version('forgetful-ai'),
                 'fresh_real_sqlite': True, 'native_startup_registration': True,
                 'before_warning_and_after_advisory_flat': True, 'read_model_unchanged': True,
                 'enforce_update_rejected_without_persistence': True,
                 'actual_installed_console_discovery': True, 'external_provider_calls': False}
        Path('/runtime/installed-task-demo.json').write_text(json.dumps(facts, indent=2))
        print(json.dumps(facts, indent=2))
    finally:
        await dispose_runtime(runtime)


asyncio.run(main())
