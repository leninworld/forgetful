"""Real installed-wheel HTTP pagination consumer outside the source checkout.

The HTTP client reaches production ASGI routes and SQLite persistence. This is
not a network-server or external-UI test. Shipping tests cover the real server.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

from fastmcp import Client, FastMCP
from httpx import ASGITransport, AsyncClient

import app
from app.bootstrap import build_runtime, dispose_runtime
from app.config.settings import settings
from app.routes.api import code_artifacts, documents, files, plans, projects, skills, tasks

INSTALLED = Path('/work/installed')
assert Path.cwd() == Path('/work/demo')
assert Path(app.__file__).resolve().is_relative_to(INSTALLED)
expected_version = ('intentionally-wrong-control-version' if sys.argv[1:] == ['--wrong-version-control']
                    else '0.0.0+pagination215')
assert importlib.metadata.version('forgetful-ai') == expected_version, 'INSTALLED_WHEEL_VERSION_MISMATCH'
settings.PLANNING_ENABLED = True
settings.FILES_ENABLED = True
settings.SKILLS_ENABLED = True
settings.ACTIVITY_ENABLED = False
settings.RERANKING_ENABLED = False
settings.FORGETFUL_SERVER = ''
settings.FORGETFUL_SCOPES = '*'
settings.DATABASE = 'SQLite'
settings.SQLITE_MEMORY = False
settings.SQLITE_PATH = '/work/demo/pagination.db'
settings.DEFAULT_USER_ID = 'installed-pagination-owner'
settings.DEFAULT_USER_NAME = 'Installed pagination consumer'
settings.DEFAULT_USER_EMAIL = 'pagination@example.test'
assert not Path(settings.SQLITE_PATH).exists()
REQUESTS = []


@asynccontextmanager
async def lifespan(server):
    runtime = await build_runtime()
    for category in ('user', 'project', 'plan', 'task', 'document', 'code_artifact', 'file', 'skill'):
        setattr(server, category + '_service', getattr(runtime.services, category))
    server.registry = runtime.registry
    server._instance_permitted_tools = runtime.permitted_tools
    server._instance_scopes = runtime.instance_scopes
    try:
        yield
    finally:
        await dispose_runtime(runtime)


async def main():
    server = FastMCP('Installed pagination consumer', lifespan=lifespan)
    for routes in (projects, plans, tasks, documents, code_artifacts, files, skills):
        routes.register(server)
    async with Client(server):
        async with AsyncClient(
            transport=ASGITransport(app=server.http_app(), raise_app_exceptions=False),
            base_url='http://installed.example.test',
        ) as http:
            async def post(resource, body):
                address = '/api/v1/' + resource.replace('_', '-')
                response = await http.post(address, json=body)
                REQUESTS.append({'method': 'POST', 'resource': resource, 'status': response.status_code})
                assert response.status_code == 201, response.text
                return response.json()

            project_rows = [await post('projects', {
                'name': 'installed-project-' + str(index), 'description': 'Synthetic consumer',
                'project_type': 'development',
            }) for index in range(3)]
            project_id = project_rows[0]['id']
            plan_rows = [await post('plans', {
                'title': 'installed-plan-' + str(index), 'project_id': project_id,
                'status': 'active',
            }) for index in range(3)]
            plan_id = plan_rows[0]['id']
            seeded = {'projects': project_rows, 'plans': plan_rows}
            specs = {
                'tasks': lambda i: {'title': 'installed-task-' + str(i), 'plan_id': plan_id},
                'documents': lambda i: {'title': 'installed-document-' + str(i),
                    'description': 'Synthetic document', 'content': 'private fixture content',
                    'project_id': project_id},
                'code_artifacts': lambda i: {'title': 'installed-code-' + str(i),
                    'description': 'Synthetic code', 'code': 'pass', 'language': 'python',
                    'project_id': project_id},
                'files': lambda i: {'filename': 'installed-file-' + str(i) + '.txt',
                    'description': 'Synthetic file', 'data': 'c3ludGhldGlj',
                    'mime_type': 'text/plain', 'project_id': project_id},
                'skills': lambda i: {'name': 'installed-skill-' + str(i),
                    'description': 'Synthetic skill', 'content': 'Synthetic instructions',
                    'project_id': project_id},
            }
            for resource, payload in specs.items():
                seeded[resource] = [await post(resource, payload(i)) for i in range(3)]
            results = {}
            for resource in ('projects', 'plans', 'tasks', 'documents', 'code_artifacts', 'files', 'skills'):
                address = '/api/v1/' + resource.replace('_', '-')
                params = {'limit': 1, 'offset': 1}
                if resource == 'tasks':
                    params['plan_id'] = plan_id
                elif resource not in ('projects', 'plans'):
                    params['project_id'] = project_id
                elif resource == 'plans':
                    params['project_id'] = project_id
                response = await http.get(address, params=params)
                REQUESTS.append({'method': 'GET', 'resource': resource, 'status': response.status_code})
                assert response.status_code == 200, response.text
                value = response.json()
                assert len(value[resource]) == 1
                assert (value['total'], value['limit'], value['offset']) == (3, 1, 1)
                assert value[resource][0]['id'] in {row['id'] for row in seeded[resource]}
                assert not {'content', 'code', 'data', 'embedding'}.intersection(value[resource][0])
                results[resource] = {'returned': 1, 'filteredTotal': value['total'],
                                     'limit': value['limit'], 'offset': value['offset'],
                                     'summaryKeys': sorted(value[resource][0])}
    # Bind the modules that actually served these requests to the installed wheel.
    tested = json.loads(Path('/runtime/tested-hashes.json').read_text())
    origins = {}
    for name, module in sorted(sys.modules.items()):
        if name == 'app' or name.startswith('app.'):
            raw_path = getattr(module, '__file__', None)
            if raw_path is None:
                continue
            path = Path(raw_path).resolve()
            assert path.is_relative_to(INSTALLED), (name, str(path))
            rel = str(path.relative_to(INSTALLED))
            if rel in tested:
                assert hashlib.sha256(path.read_bytes()).hexdigest() == tested[rel]
                origins[name] = {'path': rel, 'sha256': tested[rel]}
    assert all('app.routes.api.' + n in origins for n in (
        'projects', 'plans', 'tasks', 'documents', 'code_artifacts', 'files', 'skills'))
    facts = {'status': 'PASS', 'installed_version': importlib.metadata.version('forgetful-ai'),
             'installed_origin': str(Path(app.__file__).resolve()), 'actual_requests': REQUESTS,
             'seven_resources': results, 'installed_candidate_modules': origins,
             'productionASGIRoutesAndSQLite': True, 'separateNetworkServerExercised': False,
             'externalUIExercised': False, 'sourceCheckoutImported': False,
             'providerNetwork': False}
    phase = os.environ['INSTALLED_CONSUMER_PHASE']
    assert phase in ('early-sqlite', 'candidate')
    Path('/runtime', phase + '-installed-pagination-demo.json').write_text(json.dumps(facts, indent=2))
    print(json.dumps(facts, indent=2))


asyncio.run(main())
