"""Public synthetic demo against the installed wheel, never the checkout."""
import asyncio
import importlib.metadata
import json
from pathlib import Path
import sys
from types import ModuleType

import app
from app.bootstrap import build_runtime, dispose_runtime
from app.config.settings import settings
from app.models.plan_models import PlanCreate, PlanStatus, PlanUpdate, TaskCreate, TaskState
from app.models.project_models import ProjectCreate
from app.models.user_models import UserCreate
from app.models.write_plugin_models import PluginWarning, WriteDecision
from app.protocols.plan_plugin_protocol import PlanPlugin

assert Path(app.__file__).resolve().is_relative_to('/work/installed')
calls = []


async def before(context):
    calls.append(('before', context.operation, context.automatic))
    if context.automatic and context.previous.title == 'Synthetic reject':
        return WriteDecision(outcome='reject', message='Synthetic completion requires review.')
    return WriteDecision(outcome='allow')


async def after(context):
    calls.append(('after', context.operation, context.automatic))
    if context.automatic:
        return PluginWarning(message='Review synthetic completion.', code='SYNTHETIC_REVIEW')
    return None


module = ModuleType('synthetic_installed_plan_admin')
module.plugin = PlanPlugin(
    before_save={op: before for op in ('create', 'update', 'delete')},
    after_save={op: after for op in ('create', 'update', 'delete')},
)
sys.modules[module.__name__] = module
settings.PLAN_WRITE_PLUGIN = module.__name__ + ':plugin'
settings.PLAN_WRITE_PLUGIN_MODE = 'enforce'
settings.PLANNING_ENABLED = True
settings.DATABASE = 'SQLite'
settings.SQLITE_MEMORY = False
settings.SQLITE_PATH = '/work/demo/installed-plan.db'


async def main():
    runtime = await build_runtime()
    try:
        user = await runtime.services.user.get_or_create_user(UserCreate(
            external_id='synthetic-installed-plan', name='Synthetic user', email='synthetic@example.test',
        ))
        project = await runtime.services.project.create_project(user.id, ProjectCreate(
            name='Synthetic installed plan', description='Public validation only', project_type='development',
        ))
        plan = (await runtime.services.plan.create_plan(user.id, PlanCreate(
            project_id=project.id, title='Synthetic reject', status=PlanStatus.ACTIVE,
        ))).plan
        task = await runtime.services.task.create_task(user.id, TaskCreate(plan_id=plan.id, title='Synthetic task'))
        doing = await runtime.services.task.transition_task(user.id, task.id, TaskState.DOING, task.version)
        done = await runtime.services.task.transition_task(user.id, task.id, TaskState.DONE, doing.task.version)
        assert done.task.state == TaskState.DONE
        assert done.plugin_warnings[0].code == 'plan_auto_completion_rejected'
        assert (await runtime.services.plan.get_plan(user.id, plan.id)).status == PlanStatus.ACTIVE
        await runtime.services.plan.update_plan(user.id, plan.id, PlanUpdate(title='Synthetic allow'))
        warnings = await runtime.services.task._check_plan_auto_completion(user.id, plan.id)
        assert warnings[0].code == 'SYNTHETIC_REVIEW'
        assert (await runtime.services.plan.get_plan(user.id, plan.id)).status == PlanStatus.COMPLETED
        second = (await runtime.services.plan.create_plan(user.id, PlanCreate(
            project_id=project.id, title='Synthetic deletion completion', status=PlanStatus.ACTIVE,
        ))).plan
        first_task = await runtime.services.task.create_task(user.id, TaskCreate(plan_id=second.id, title='Done'))
        blocker = await runtime.services.task.create_task(user.id, TaskCreate(plan_id=second.id, title='Delete'))
        doing = await runtime.services.task.transition_task(user.id, first_task.id, TaskState.DOING, first_task.version)
        await runtime.services.task.transition_task(user.id, first_task.id, TaskState.DONE, doing.task.version)
        removed = await runtime.services.task.delete_task(user.id, blocker.id)
        assert removed.success and removed.plugin_warnings[0].code == 'SYNTHETIC_REVIEW'
        assert (await runtime.services.plan.get_plan(user.id, second.id)).status == PlanStatus.COMPLETED
        deleted = await runtime.services.plan.delete_plan(user.id, plan.id)
        assert deleted.success and ('after', 'delete', False) in calls
        assert all(any(row[:2] == (phase, operation) for row in calls)
                   for phase in ('before', 'after') for operation in ('create', 'update', 'delete'))
        facts = dict(installed_wheel=importlib.metadata.version('forgetful-ai'),
                     plan_crud=True, transition_rejection_keeps_task=True,
                     successful_completion_warning=True, deletion_completion=True,
                     synthetic_only=True, external_provider_calls=False)
        Path('/runtime/installed-demo.json').write_text(json.dumps(facts, indent=2))
        print(json.dumps(facts, indent=2))
    finally:
        await dispose_runtime(runtime)


asyncio.run(main())
