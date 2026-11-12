"""Proposed installed-wheel demonstration; execute only in the reviewed Docker packet."""
import asyncio
import base64
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

import app
from app.bootstrap import build_runtime, dispose_runtime
from app.config.settings import settings
from app.models.code_artifact_models import CodeArtifactCreate
from app.models.document_models import DocumentCreate
from app.models.file_models import FileCreate
from app.models.skill_bundle_models import SkillBundle
from app.models.user_models import UserCreate

assert Path(app.__file__).resolve().is_relative_to('/work/installed')
assert Path.cwd() == Path('/work/demo')
settings.SKILLS_ENABLED = True
settings.FILES_ENABLED = True
settings.ACTIVITY_ENABLED = False
settings.RERANKING_ENABLED = False
settings.FORGETFUL_SERVER = ''
settings.DATABASE = 'SQLite'
settings.SQLITE_MEMORY = False
settings.FORGETFUL_SCOPES = '*'
PACKAGE = SkillBundle.model_validate({
    'schema_version': 1,
    'skill_md': ('---\nname: installed-portable\ndescription: Installed bundle demo\n'
                 'metadata:\n  author: synthetic-example\n---\n\n'
                 'Read [reference](references/nested/guide.md), run [script](scripts/run.py), '
                 'and use [asset](assets/icon.bin).'),
    'resources': [
        {'path': 'references/nested/guide.md', 'kind': 'document', 'title': 'Guide',
         'description': 'Portable reference', 'content': '  café reference\n', 'document_type': 'markdown'},
        {'path': 'scripts/run.py', 'kind': 'code_artifact', 'title': 'Script',
         'description': 'Portable script, never executed', 'code': "print('synthetic')\n", 'language': 'Python'},
        {'path': 'assets/icon.bin', 'kind': 'file', 'description': 'Portable binary',
         'data': base64.b64encode(b'\x00\xffsynthetic-asset').decode(), 'mime_type': 'application/octet-stream'},
    ],
})


async def owner(runtime):
    return await runtime.services.user.get_or_create_user(UserCreate(
        external_id='installed-bundle-owner', name='Synthetic owner', email='bundle@example.test',
    ))


async def main():
    settings.SQLITE_PATH = '/work/demo/source.db'
    assert not Path(settings.SQLITE_PATH).exists()
    runtime = await build_runtime()
    try:
        user = await owner(runtime)
        created = await runtime.services.skill.import_skill_bundle(user.id, PACKAGE)
        exported = await runtime.services.skill.export_skill_bundle(user.id, created.skill.id, allowed_kinds=frozenset({"document", "code_artifact", "file"}))
        original_ids = {member.path: member.resource_id for member in created.members}
        assert len(exported.resources) == 3
    finally:
        await dispose_runtime(runtime)
    settings.SQLITE_PATH = '/work/demo/target.db'
    assert not Path(settings.SQLITE_PATH).exists()
    target = await build_runtime()
    try:
        user = await owner(target)
        await target.services.document.create_document(user.id, DocumentCreate(
            title='Target seed', description='Independent destination', content='seed',
        ))
        await target.services.code_artifact.create_code_artifact(user.id, CodeArtifactCreate(
            title='Target seed', description='Independent destination', code='pass', language='python',
        ))
        await target.services.file.create_file(user.id, FileCreate(
            filename='seed.bin', description='Independent destination', data='c2VlZA==',
            mime_type='application/octet-stream',
        ))
        imported = await target.services.skill.import_skill_bundle(user.id, exported)
        assert all(original_ids[member.path] != member.resource_id for member in imported.members)
        roundtrip = await target.services.skill.export_skill_bundle(user.id, imported.skill.id, allowed_kinds=frozenset({"document", "code_artifact", "file"}))
        assert roundtrip.model_dump() == exported.model_dump()
        binary = next(resource for resource in roundtrip.resources if resource.kind == 'file')
        assert base64.b64decode(binary.data) == b'\x00\xffsynthetic-asset'
        # The console uses its default user, which must not see this synthetic owner.
        console_env = dict(os.environ, PYTHONPATH='/work/installed:/packet',
                           SKILLS_ENABLED='true', FILES_ENABLED='true', DATABASE='SQLite',
                           SQLITE_MEMORY='false', SQLITE_PATH=settings.SQLITE_PATH,
                           FORGETFUL_SERVER='', FORGETFUL_SCOPES='*')
        checked = subprocess.run(  # noqa: S603 - fixed installed console and synthetic args
            [sys.executable, '/work/installed/bin/forgetful', 'tools', 'info',
             'import_skill_bundle', '--local', '--json'],
            capture_output=True, text=True, env=console_env, timeout=30, check=False,
        )
        assert checked.returncode == 0, checked.stderr
        assert json.loads(checked.stdout)['name'] == 'import_skill_bundle'
        facts = {'status': 'PASS', 'installed_version': importlib.metadata.version('forgetful-ai'),
                 'installed_origin': str(Path(app.__file__).resolve()),
                 'independent_source_and_target_databases': True,
                 'all_destination_resource_ids_differ': True,
                 'document_script_binary_roundtrip': True,
                 'second_export_identical': True, 'binary_bytes_identical': True,
                 'actual_installed_console_discovery': True,
                 'external_provider_calls': False, 'scripts_executed': False}
        Path('/runtime/installed-bundle-demo.json').write_text(json.dumps(facts, indent=2))
        print(json.dumps(facts, indent=2))
    finally:
        await dispose_runtime(target)


asyncio.run(main())
