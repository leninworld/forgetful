"""Installed-wheel public SkillPlugin behavior against a real in-memory database."""
import asyncio
import json
from pathlib import Path

import app
from app.config.settings import settings
from app.bootstrap import get_embedding_adapter
from app.exceptions import WritePluginRejectedError
from app.models.skill_models import SkillCreate
from app.models.user_models import UserCreate
from app.models.write_plugin_models import PluginWarning, WriteDecision
from app.protocols.skill_plugin_protocol import SkillPlugin
from app.repositories.sqlite.skill_repository import SqliteSkillRepository
from app.repositories.sqlite.sqlite_adapter import SqliteDatabaseAdapter
from app.repositories.sqlite.user_repository import SqliteUserRepository
from app.routes.skill_plugin_responses import skill_write_response
from app.services.skill_plugin_service import SkillPluginService
from app.services.skill_service import SkillService
from app.services.user_service import UserService

assert Path(app.__file__).resolve().is_relative_to(Path('/work/installed'))
settings.DATABASE = 'SQLite'
settings.SQLITE_MEMORY = True
settings.SKILLS_ENABLED = True
calls = []


async def before(request):
    calls.append(['before', request.operation])
    if request.proposed.content == 'blocked':
        return WriteDecision(outcome='reject', message='Synthetic policy rejection.')
    return WriteDecision(outcome='allow')


async def after(request):
    calls.append(['after', request.operation])
    return PluginWarning(message='Synthetic saved-skill advisory.')


async def main():
    db = SqliteDatabaseAdapter()
    await db.init_db()
    try:
        user = await UserService(SqliteUserRepository(db)).get_or_create_user(UserCreate(
            external_id='installed-skill-policy', name='Synthetic user', email='test@example.com',
        ))
        repo = SqliteSkillRepository(db, get_embedding_adapter())
        service = SkillService(repo, write_plugin=SkillPluginService(SkillPlugin(
            before_save={'create': before}, after_save={'create': after},
        )))
        made = await service.create_skill(user.id, SkillCreate(
            name='installed-skill', description='Synthetic installed API check', content='allowed',
        ))
        response = skill_write_response(made).model_dump(mode='json')
        assert response['content'] == 'allowed' and response['plugin_warnings']
        assert 'skill' not in response
        read = await repo.get_skill_by_id(user.id, made.skill.id)
        assert 'plugin_warnings' not in read.model_dump()
        try:
            await service.create_skill(user.id, SkillCreate(
                name='blocked-installed-skill', description='Synthetic rejection', content='blocked',
            ))
        except WritePluginRejectedError:
            pass
        else:
            raise AssertionError('Expected the installed policy to reject before persistence')
        assert not await repo.skill_name_exists(user.id, 'blocked-installed-skill')
        assert calls == [['before', 'create'], ['after', 'create'], ['before', 'create']]
        print(json.dumps({'installed_package': str(app.__file__), 'saved': response['name'],
                          'advisory': response['plugin_warnings'][0]['message'],
                          'rejected_not_persisted': True, 'calls': calls}), flush=True)
    finally:
        await db.dispose()


asyncio.run(main())
