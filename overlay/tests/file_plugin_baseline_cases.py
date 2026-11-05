"""Behavioral oracles usable unchanged on original main and the candidate.

No imports from new File policy modules: baseline failures must be assertions.
"""
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.file_models import FileCreate, FileUpdate
from app.models.project_models import ProjectCreate
from app.models.user_models import UserCreate
from app.services.file_service import FileService
from app.services.user_service import UserService


def value():
    return FileCreate(filename="compat.bin", description="Compatibility", mime_type="text/plain",
                      data="YWJj", tags=["fixture"], source_repo="synthetic",
                      source_files=["fixture.py"], source_url="https://example.test/fixture",
                      confidence=0.5, encoding_agent="synthetic", encoding_version="1",
                      agent_id="fixture-agent", agent_version="1", agent_model="synthetic")


def unwrap(result):
    return getattr(result, "file", result)


class Rejected(Exception):
    pass


class RecordingPolicy:
    def __init__(self, reject=False):
        self.before = []
        self.after = []
        self.reject = reject
    def has_before(self, operation):
        return True
    def has_after(self, operation):
        return True
    async def before_save(self, request):
        self.before.append(request)
        if self.reject:
            raise Rejected("Synthetic policy rejection")
        return []
    async def after_save(self, request):
        self.after.append(request)
        return []


class Bus:
    def __init__(self):
        self.events = []
    async def emit(self, event):
        self.events.append(event)


async def make_baseline_store(db, backend):
    if backend == "SQLite":
        from app.repositories.sqlite.file_repository import SqliteFileRepository as Files
        from app.repositories.sqlite.project_repository import SqliteProjectRepository as Projects
        from app.repositories.sqlite.user_repository import SqliteUserRepository as Users
        from app.repositories.sqlite.sqlite_tables import FilesTable
    else:
        from app.repositories.postgres.file_repository import PostgresFileRepository as Files
        from app.repositories.postgres.project_repository import PostgresProjectRepository as Projects
        from app.repositories.postgres.user_repository import PostgresUserRepository as Users
        from app.repositories.postgres.postgres_tables import FilesTable
    users = Users(db)
    user = await UserService(users).get_or_create_user(UserCreate(
        external_id=str(uuid4()), name="File baseline", email=f"{uuid4()}@example.test",
    ))
    return SimpleNamespace(db=db, backend=backend, repo=Files(db), projects=Projects(db),
                           user_id=user.id, table=FilesTable)


def service(store, plugin=None, bus=None):
    kwargs = {"event_bus": bus}
    if "file_plugin" in inspect.signature(FileService).parameters:
        kwargs.update(file_plugin=plugin, project_repo=store.projects)
    return FileService(store.repo, **kwargs)


def observe(record):
    path = os.environ.get("FORGETFUL_FILE_COMPAT_OUTPUT")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, default=str) + "\n")


class FileBaselineCases:
    async def test_create_dispatches_confirmed_phases(self, baseline_store):
        store = baseline_store
        policy = RecordingPolicy()
        saved = unwrap(await service(store, policy).create_file(store.user_id, value()))
        assert saved.filename == "compat.bin"
        assert len(policy.before) == len(policy.after) == 1

    async def test_rejection_never_persists(self, baseline_store):
        store = baseline_store
        with pytest.raises(Rejected):
            await service(store, RecordingPolicy(reject=True)).create_file(store.user_id, value())
        assert await store.repo.list_files(store.user_id) == []

    async def test_update_dispatches_only_actual_content_change(self, baseline_store):
        store = baseline_store
        saved = unwrap(await service(store).create_file(store.user_id, value()))
        policy = RecordingPolicy()
        result = unwrap(await service(store, policy).update_file(store.user_id, saved.id, FileUpdate(data="eHl6")))
        assert result.data == "eHl6"
        assert len(policy.before) == len(policy.after) == 1
        assert "data" in policy.after[0].changed_fields

    async def test_delete_reports_confirmed_removal(self, baseline_store):
        store = baseline_store
        saved = unwrap(await service(store).create_file(store.user_id, value()))
        policy = RecordingPolicy()
        await service(store, policy).delete_file(store.user_id, saved.id)
        assert await store.repo.get_file_by_id(store.user_id, saved.id) is None
        assert len(policy.after) == 1
        assert policy.after[0].deleted_file_id == saved.id

    async def test_original_persistence_positive_control(self, baseline_store):
        store = baseline_store
        saved = unwrap(await service(store).create_file(store.user_id, value()))
        assert (await store.repo.get_file_by_id(store.user_id, saved.id)).data == "YWJj"

    @pytest.mark.parametrize("field", ["filename", "description", "mime_type", "data", "tags", "project_id",
                                       "source_repo", "source_files", "source_url", "confidence", "encoding_agent",
                                       "encoding_version", "agent_id", "agent_version", "agent_model"])
    async def test_null_observation_preserves_core(self, baseline_store, field):
        store = baseline_store
        project = await store.projects.create_project(store.user_id, ProjectCreate(
            name="Null compatibility", description="Synthetic ownership", project_type="development",
        ))
        data = value()
        data.project_id = project.id
        saved = unwrap(await service(store).create_file(store.user_id, data))
        error = None
        try:
            await service(store).update_file(store.user_id, saved.id, FileUpdate(**{field: None}))
        except Exception as exc:
            error = type(exc).__name__
        async with store.db.session(store.user_id) as session:
            result = await session.execute(select(getattr(store.table, field)).where(store.table.id == saved.id))
            persisted = result.scalar_one()
        observe({"kind": "null", "backend": store.backend, "field": field,
                 "error_type": error, "persisted": persisted})
        if field != "tags":
            assert len(await store.repo.list_files(store.user_id)) == 1

    @pytest.mark.parametrize("patch", [{}, {"description": "Changed"}, {"data": "eHl6"}, {"data": "Y!WJj"}])
    async def test_no_plugin_event_and_timestamp_compatibility(self, baseline_store, patch):
        store = baseline_store
        bus = Bus()
        core = service(store, bus=bus)
        saved = unwrap(await core.create_file(store.user_id, value()))
        assert bus.events[-1].snapshot == saved.model_dump(mode="json", exclude={"data"})
        bus.events.clear()
        updated = unwrap(await core.update_file(store.user_id, saved.id, FileUpdate(**patch)))
        assert updated.updated_at > saved.updated_at
        assert len(bus.events) == (1 if "description" in patch else 0)
        if bus.events:
            assert bus.events[0].snapshot == updated.model_dump(mode="json", exclude={"data"})
            assert bus.events[0].changes == {"description": {"old": "Compatibility", "new": "Changed"}}
        observe({"kind": "event", "backend": store.backend, "patch": patch,
                 "event_count": len(bus.events), "changes": [e.changes for e in bus.events],
                 "snapshot_keys": sorted(updated.model_dump(exclude={"data"})), "timestamp_bumped": True})
        bus.events.clear()
        await core.delete_file(store.user_id, saved.id)
        assert bus.events[0].snapshot == updated.model_dump(mode="json", exclude={"data"})
