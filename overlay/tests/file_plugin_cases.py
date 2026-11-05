"""Shared real SQLite/PostgreSQL File policy contracts."""
import asyncio
import base64
import logging
from dataclasses import dataclass
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.exceptions import NotFoundError, WritePluginRejectedError, WritePluginUnavailableError
from app.models.file_models import FileCreate, FileUpdate
from app.models.project_models import ProjectCreate
from app.models.user_models import UserCreate
from app.models.write_plugin_models import WriteDecision
from app.protocols.file_plugin_protocol import FilePlugin
from app.services.file_service import FileService
from app.services.file_write_plugin_service import FileWritePluginService
from app.services.user_service import UserService


def candidate(**changes):
    return FileCreate(**({"filename": "note.bin", "description": "Synthetic policy fixture",
                         "mime_type": "application/octet-stream", "data": "YWJj", "tags": []} | changes))


@dataclass
class Store:
    db: object
    repo: object
    projects: object
    users: object
    user_id: object
    backend: str

    def service(self, plugin=None, mode="enforce", timeout=5, bus=None):
        runner = None if plugin is None else FileWritePluginService(plugin, mode, timeout)
        return FileService(self.repo, event_bus=bus, file_plugin=runner, project_repo=self.projects)

    async def other_user(self):
        return await UserService(self.users).get_or_create_user(UserCreate(
            external_id=str(uuid4()), name="Other file owner", email=f"{uuid4()}@example.test",
        ))

    async def project(self, user_id):
        return await self.projects.create_project(user_id, ProjectCreate(
            name="File scope", description="Synthetic project", project_type="development",
        ))


async def make_store(db, backend):
    if backend == "SQLite":
        from app.repositories.sqlite.file_repository import SqliteFileRepository as Files
        from app.repositories.sqlite.project_repository import SqliteProjectRepository as Projects
        from app.repositories.sqlite.user_repository import SqliteUserRepository as Users
    else:
        from app.repositories.postgres.file_repository import PostgresFileRepository as Files
        from app.repositories.postgres.project_repository import PostgresProjectRepository as Projects
        from app.repositories.postgres.user_repository import PostgresUserRepository as Users
    users = Users(db)
    user = await UserService(users).get_or_create_user(UserCreate(
        external_id=str(uuid4()), name="File policy owner", email=f"{uuid4()}@example.test",
    ))
    return Store(db, Files(db), Projects(db), users, user.id, backend)


class FilePluginCases:
    async def test_all_phases_content_and_detached_state(self, file_store):
        store = file_store
        seen = []

        async def before(ctx):
            seen.append(ctx.model_copy(deep=True))
            if ctx.proposed is not None:
                ctx.proposed.filename = "must-not-persist"
                ctx.proposed.tags.append("must-not-persist")
            ctx.submitted.metadata.description = "must-not-persist"
            return WriteDecision(outcome="allow")

        async def after(ctx):
            seen.append(ctx.model_copy(deep=True))
            if hasattr(ctx, "saved"):
                ctx.saved.tags.append("must-not-persist")

        plugin = FilePlugin(before_save={op: before for op in ("create", "update", "delete")},
                            after_save={op: after for op in ("create", "update", "delete")})
        service = store.service(plugin)
        saved = (await service.create_file(store.user_id, candidate())).file
        assert saved.filename == "note.bin" and saved.tags == []
        assert seen[0].content == b"abc" and len(seen[0].content) == saved.size_bytes
        assert "data" not in seen[1].saved.model_dump()
        updated = (await service.update_file(store.user_id, saved.id, FileUpdate(description="Changed"))).file
        assert updated.description == "Changed" and updated.tags == []
        assert seen[2].content is None and seen[2].content_availability == "not_submitted"
        assert seen[2].previous.description == "Synthetic policy fixture"
        await service.update_file(store.user_id, saved.id, FileUpdate(data="eHl6"))
        assert seen[4].content == b"xyz"
        assert "data" in seen[5].changed_fields
        assert seen[5].previous.size_bytes == seen[5].saved.size_bytes == 3
        result = await service.delete_file(store.user_id, saved.id)
        assert result.deleted and len(seen) == 8
        assert seen[-1].deleted_file_id == saved.id and not hasattr(seen[-1], "saved")
        assert await store.repo.get_file_by_id(store.user_id, saved.id) is None

    @pytest.mark.parametrize("operation", ["create", "update", "delete"])
    @pytest.mark.parametrize("mode", ["warn", "enforce"])
    @pytest.mark.parametrize("outcome", ["allow", "warn", "reject"])
    async def test_before_decision(self, file_store, operation, mode, outcome):
        store = file_store
        original = (await store.service().create_file(store.user_id, candidate())).file

        async def before(ctx):
            return WriteDecision(outcome=outcome, message="Administrator decision")

        service = store.service(FilePlugin(before_save={operation: before}), mode)
        async def mutate():
            if operation == "create":
                return await service.create_file(store.user_id, candidate())
            if operation == "update":
                return await service.update_file(store.user_id, original.id, FileUpdate(description="New"))
            return await service.delete_file(store.user_id, original.id)
        if outcome == "reject" and mode == "enforce":
            with pytest.raises(WritePluginRejectedError, match="Administrator decision"):
                await mutate()
            assert (await store.repo.get_file_by_id(store.user_id, original.id)).description == original.description
            assert len(await store.repo.list_files(store.user_id)) == 1
        else:
            result = await mutate()
            assert bool(result.plugin_warnings) == (outcome != "allow")
            if operation == "delete":
                assert result.deleted

    @pytest.mark.parametrize("phase", ["before", "after"])
    @pytest.mark.parametrize("mode", ["warn", "enforce"])
    @pytest.mark.parametrize("failure", ["error", "timeout", "malformed", "child_cancel"])
    async def test_failure_semantics_and_sanitized_logs(self, file_store, phase, mode, failure, caplog):
        store = file_store
        logging.getLogger("app.services.file_write_plugin_service").disabled = False
        async def handler(ctx):
            if failure == "error":
                raise RuntimeError("SECRET YWJj")
            if failure == "timeout":
                await asyncio.Event().wait()
            if failure == "child_cancel":
                child = asyncio.create_task(asyncio.Event().wait())
                child.cancel("SECRET YWJj")
                await child
            return {"outcome": "reject"} if phase == "after" else {"outcome": "unknown", "input": "SECRET YWJj"}
        plugin = FilePlugin(**{f"{phase}_save": {"create": handler}})
        service = store.service(plugin, mode, timeout=0.01 if failure == "timeout" else 5)
        if phase == "before" and mode == "enforce":
            with pytest.raises(WritePluginUnavailableError, match="File Write Plugin unavailable"):
                await service.create_file(store.user_id, candidate())
        else:
            result = await service.create_file(store.user_id, candidate())
            assert result.plugin_warnings
            assert await store.repo.get_file_by_id(store.user_id, result.file.id) is not None
            assert "SECRET" not in str(result.plugin_warnings)
        assert "SECRET" not in caplog.text and "YWJj" not in caplog.text
        assert "error_type=" in caplog.text

    @pytest.mark.parametrize("phase", ["before", "after"])
    @pytest.mark.parametrize("operation", ["create", "update", "delete"])
    async def test_genuine_cancellation(self, file_store, phase, operation):
        store = file_store
        saved = None if operation == "create" else (await store.service().create_file(store.user_id, candidate())).file
        entered = asyncio.Event()
        async def handler(ctx):
            entered.set()
            await asyncio.Event().wait()
        service = store.service(FilePlugin(**{f"{phase}_save": {operation: handler}}))
        if operation == "create":
            action = service.create_file(store.user_id, candidate())
        elif operation == "update":
            action = service.update_file(store.user_id, saved.id, FileUpdate(description="Committed"))
        else:
            action = service.delete_file(store.user_id, saved.id)
        task = asyncio.create_task(action)
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        rows = await store.repo.list_files(store.user_id)
        if operation == "create":
            assert len(rows) == (1 if phase == "after" else 0)
        elif operation == "delete":
            assert len(rows) == (0 if phase == "after" else 1)
        else:
            assert rows[0].description == ("Committed" if phase == "after" else saved.description)

    async def test_missing_foreign_noop_and_same_bytes(self, file_store):
        store = file_store
        calls = []
        async def before(ctx):
            calls.append(ctx)
            return WriteDecision(outcome="allow")
        async def after(ctx):
            calls.append(ctx)
        service = store.service(FilePlugin(before_save={"update": before, "delete": before},
                                           after_save={"update": after, "delete": after}))
        saved = (await service.create_file(store.user_id, candidate())).file
        for patch in [FileUpdate(), FileUpdate(description=saved.description), FileUpdate(data="Y!WJj")]:
            result = await service.update_file(store.user_id, saved.id, patch)
            assert result.file.updated_at >= saved.updated_at
        assert calls == []
        other = await store.other_user()
        with pytest.raises(NotFoundError):
            await service.update_file(other.id, saved.id, FileUpdate(description="Forbidden"))
        assert not (await service.delete_file(other.id, saved.id)).deleted
        assert not (await service.delete_file(store.user_id, 2147483647)).deleted
        assert calls == []

    async def test_project_ownership_precedes_before(self, file_store):
        store = file_store
        foreign = await store.other_user()
        project = await store.project(foreign.id)
        calls = []
        async def before(ctx):
            calls.append(ctx)
            return WriteDecision(outcome="allow")
        service = store.service(FilePlugin(before_save={"create": before, "update": before}))
        with pytest.raises(NotFoundError, match=f"Project {project.id} not found"):
            await service.create_file(store.user_id, candidate(project_id=project.id))
        saved = (await store.service().create_file(store.user_id, candidate())).file
        with pytest.raises(NotFoundError, match=f"Project {project.id} not found"):
            await service.update_file(store.user_id, saved.id, FileUpdate(project_id=project.id))
        assert calls == []

    async def test_refresh_preserves_unrelated_update_and_delete_snapshot(self, file_store):
        store = file_store
        core = store.service()
        saved = (await core.create_file(store.user_id, candidate())).file
        entered, release = asyncio.Event(), asyncio.Event()
        contexts = []
        async def before(ctx):
            entered.set()
            await release.wait()
            return WriteDecision(outcome="allow")
        async def after(ctx):
            contexts.append(ctx)
        service = store.service(FilePlugin(before_save={"update": before, "delete": before},
                                           after_save={"update": after, "delete": after}))
        pending = asyncio.create_task(service.update_file(store.user_id, saved.id, FileUpdate(description="Own")))
        await asyncio.wait_for(entered.wait(), 5)
        await core.update_file(store.user_id, saved.id, FileUpdate(filename="concurrent.bin"))
        release.set()
        result = await asyncio.wait_for(pending, 5)
        assert result.file.filename == "concurrent.bin" and result.file.description == "Own"
        assert contexts[0].previous.filename == "concurrent.bin"
        entered.clear()
        release.clear()
        pending = asyncio.create_task(service.delete_file(store.user_id, saved.id))
        await asyncio.wait_for(entered.wait(), 5)
        await core.update_file(store.user_id, saved.id, FileUpdate(description="Fresh deletion"))
        release.set()
        assert (await asyncio.wait_for(pending, 5)).deleted
        assert contexts[-1].previous.description == "Fresh deletion"

    async def test_missing_after_before_has_no_after(self, file_store):
        store = file_store
        core = store.service()
        saved = (await core.create_file(store.user_id, candidate())).file
        afters = []
        async def before(ctx):
            await core.delete_file(store.user_id, saved.id)
            return WriteDecision(outcome="allow")
        async def after(ctx):
            afters.append(ctx)
        service = store.service(FilePlugin(before_save={"delete": before}, after_save={"delete": after}))
        assert not (await service.delete_file(store.user_id, saved.id)).deleted
        assert afters == []

    async def test_metadata_query_excludes_blob(self, file_store):
        store = file_store
        saved = (await store.service().create_file(store.user_id, candidate())).file
        statements = []
        def record(conn, cursor, statement, params, context, executemany):
            statements.append(statement)
        event.listen(store.db._engine.sync_engine, "before_cursor_execute", record)
        try:
            metadata = await store.repo.get_file_metadata_by_id(store.user_id, saved.id)
        finally:
            event.remove(store.db._engine.sync_engine, "before_cursor_execute", record)
        assert metadata.model_dump(mode="json") == saved.model_dump(mode="json", exclude={"data"})
        selects = [s.lower().split("from")[0] for s in statements if "from files" in s.lower()]
        assert selects and all("files.data" not in s for s in selects)

    @pytest.mark.parametrize("data", ["YWJj", "eHl6"])
    async def test_submitted_decoded_content_matches_saved_size(self, file_store, data):
        store = file_store
        contents = []
        async def before(ctx):
            contents.append(ctx.content)
            return WriteDecision(outcome="allow")
        saved = (await store.service(FilePlugin(before_save={"create": before})).create_file(
            store.user_id, candidate(data=data),
        )).file
        assert contents == [base64.b64decode(data)]
        assert len(contents[0]) == saved.size_bytes


    async def test_zero_decoded_content_reaches_before_as_empty_bytes(self, file_store):
        store = file_store
        before_calls = []
        after_calls = []
        async def before(ctx):
            before_calls.append(ctx.model_copy(deep=True))
            return WriteDecision(outcome="reject", message="Empty content rejected")
        async def after(ctx):
            after_calls.append(ctx)
        service = store.service(FilePlugin(before_save={"create": before}, after_save={"create": after}))
        with pytest.raises(WritePluginRejectedError, match="Empty content rejected"):
            await service.create_file(store.user_id, candidate(data="!!!!!"))
        assert len(before_calls) == 1
        assert before_calls[0].content == b""
        assert before_calls[0].content_availability == "submitted"
        assert before_calls[0].proposed.size_bytes == 0
        assert after_calls == []
        assert await store.repo.list_files(store.user_id) == []


    async def test_provenance_defaults_preserve_submitted_and_omitted(self, file_store, monkeypatch):
        from app.config.settings import settings
        store = file_store
        saved = (await store.service().create_file(store.user_id, candidate())).file
        calls = []
        async def before(ctx):
            calls.append(ctx)
            return WriteDecision(outcome="allow")
        service = store.service(FilePlugin(before_save={"update": before}))
        monkeypatch.setattr(settings, "ENFORCE_ENV_OVERWRITE", True)
        monkeypatch.setattr(settings, "AGENT_ID", "core-default")
        await service.update_file(store.user_id, saved.id, FileUpdate(description=saved.description))
        assert calls == []
        result = await service.update_file(store.user_id, saved.id, FileUpdate(agent_id="submitted-agent"))
        assert result.file.agent_id == "core-default"
        assert calls[0].submitted.metadata.agent_id == "submitted-agent"
        assert calls[0].proposed.agent_id == "core-default"

    async def test_maximum_decoded_content_and_validation_before_hooks(self, file_store):
        from app.config.settings import settings
        from pydantic import ValidationError
        store = file_store
        seen = []
        async def before(ctx):
            seen.append(len(ctx.content))
            return WriteDecision(outcome="allow")
        service = store.service(FilePlugin(before_save={"create": before}))
        maximum = b"x" * settings.FILE_MAX_SIZE_BYTES
        saved = (await service.create_file(store.user_id, candidate(
            data=base64.b64encode(maximum).decode(),
        ))).file
        assert saved.size_bytes == settings.FILE_MAX_SIZE_BYTES and seen == [len(maximum)]
        with pytest.raises(ValidationError):
            candidate(data=base64.b64encode(maximum + b"x").decode())
        assert seen == [len(maximum)]

    async def test_project_and_user_cascades_do_not_dispatch_hooks(self, file_store):
        from sqlalchemy import delete
        store = file_store
        project = await store.project(store.user_id)
        calls = []
        async def before(ctx):
            calls.append(ctx)
            return WriteDecision(outcome="allow")
        async def after(ctx):
            calls.append(ctx)
        service = store.service(FilePlugin(before_save={"delete": before}, after_save={"delete": after}))
        saved = (await service.create_file(store.user_id, candidate(project_id=project.id))).file
        assert await store.projects.delete_project(store.user_id, project.id)
        assert (await store.repo.get_file_by_id(store.user_id, saved.id)).project_id is None
        if store.backend == "SQLite":
            from app.repositories.sqlite.sqlite_tables import UsersTable
            user_id = str(store.user_id)
        else:
            from app.repositories.postgres.postgres_tables import UsersTable
            user_id = store.user_id
        async with store.db.session(store.user_id) as session:
            await session.execute(delete(UsersTable).where(UsersTable.id == user_id))
            await session.commit()
        assert await store.repo.get_file_by_id(store.user_id, saved.id) is None
        assert calls == []

    async def test_file_deletion_cleans_all_three_link_types(self, file_store):
        from sqlalchemy import select
        from app.config.settings import settings
        from app.models.entity_models import EntityCreate
        from app.models.memory_models import MemoryCreate
        from app.models.skill_models import SkillCreate
        store = file_store
        if store.backend == "SQLite":
            from app.repositories.sqlite.memory_repository import SqliteMemoryRepository as Memories
            from app.repositories.sqlite.entity_repository import SqliteEntityRepository as Entities
            from app.repositories.sqlite.skill_repository import SqliteSkillRepository as Skills
            from app.repositories.sqlite.sqlite_tables import (
                entity_file_association, memory_file_association, skill_file_association,
            )
        else:
            from app.repositories.postgres.memory_repository import PostgresMemoryRepository as Memories
            from app.repositories.postgres.entity_repository import PostgresEntityRepository as Entities
            from app.repositories.postgres.skill_repository import PostgresSkillRepository as Skills
            from app.repositories.postgres.postgres_tables import (
                entity_file_association, memory_file_association, skill_file_association,
            )
        class Embeddings:
            async def generate_embedding(self, text):
                return [1.0] + [0.0] * (settings.EMBEDDING_DIMENSIONS - 1)
        saved = (await store.service().create_file(store.user_id, candidate())).file
        memories = Memories(store.db, Embeddings())
        entities = Entities(store.db)
        skills = Skills(store.db, Embeddings())
        memory = await memories.create_memory(store.user_id, MemoryCreate(
            title="Linked file", content="Synthetic", context="Cascade test", keywords=[], tags=[],
            file_ids=[saved.id],
        ))
        entity = await entities.create_entity(store.user_id, EntityCreate(name="Linked", entity_type="Organization"))
        skill = await skills.create_skill(store.user_id, SkillCreate(
            name="linked-file", description="Cascade test", content="Synthetic instructions",
        ))
        await skills.link_skill_to_file(store.user_id, skill.id, saved.id)
        async with store.db.session(store.user_id) as session:
            await session.execute(entity_file_association.insert().values(entity_id=entity.id, file_id=saved.id))
            await session.commit()
        tables = [memory_file_association, entity_file_association, skill_file_association]
        async def counts():
            async with store.db.session(store.user_id) as session:
                return [len((await session.execute(select(t).where(t.c.file_id == saved.id))).all()) for t in tables]
        assert await counts() == [1, 1, 1]
        assert (await store.service(FilePlugin()).delete_file(store.user_id, saved.id)).deleted
        assert await counts() == [0, 0, 0]
        assert await entities.get_entity_by_id(store.user_id, entity.id) is not None
        assert await skills.get_skill_by_id(store.user_id, skill.id) is not None
        assert await memories.get_memory_by_id(store.user_id, memory.id) is not None


    async def test_unregistered_operation_adds_no_policy_reads(self, file_store, monkeypatch):
        store = file_store
        async def forbidden(*args, **kwargs):
            raise AssertionError("Unregistered operation performed policy work")
        monkeypatch.setattr(store.projects, "get_project_by_id", forbidden)
        monkeypatch.setattr(store.repo, "get_file_metadata_by_id", forbidden)
        service = store.service(FilePlugin(before_save={"delete": forbidden}))
        saved = (await service.create_file(store.user_id, candidate())).file
        result = await service.update_file(store.user_id, saved.id, FileUpdate(description="Allowed"))
        assert result.file.description == "Allowed" and result.plugin_warnings == []

    async def test_missing_update_after_before_never_dispatches_after(self, file_store):
        store = file_store
        saved = (await store.service().create_file(store.user_id, candidate())).file
        calls = []
        async def before(ctx):
            await store.service().delete_file(store.user_id, saved.id)
            return WriteDecision(outcome="allow")
        async def after(ctx):
            calls.append(ctx)
        service = store.service(FilePlugin(before_save={"update": before}, after_save={"update": after}))
        with pytest.raises(NotFoundError):
            await service.update_file(store.user_id, saved.id, FileUpdate(description="Late"))
        assert calls == []

    @pytest.mark.parametrize("operation", ["update", "delete"])
    async def test_post_mutation_error_does_not_undo_operation(self, file_store, operation):
        store = file_store
        saved = (await store.service().create_file(store.user_id, candidate())).file
        async def after(ctx):
            raise RuntimeError("SECRET synthetic binary")
        service = store.service(FilePlugin(after_save={operation: after}))
        if operation == "update":
            result = await service.update_file(store.user_id, saved.id, FileUpdate(description="Confirmed"))
            assert (await store.repo.get_file_by_id(store.user_id, saved.id)).description == "Confirmed"
        else:
            result = await service.delete_file(store.user_id, saved.id)
            assert result.deleted and await store.repo.get_file_by_id(store.user_id, saved.id) is None
        assert result.plugin_warnings[0].code == "FILE_PLUGIN_AFTER_SAVE_UNAVAILABLE"
        assert "SECRET" not in str(result.plugin_warnings)


    async def test_concurrent_actor_makes_patch_noop_after_before(self, file_store):
        store = file_store
        core = store.service()
        saved = (await core.create_file(store.user_id, candidate())).file
        afters = []
        async def before(ctx):
            await core.update_file(store.user_id, saved.id, FileUpdate(description="Same target"))
            return WriteDecision(outcome="allow")
        async def after(ctx):
            afters.append(ctx)
        service = store.service(FilePlugin(before_save={"update": before}, after_save={"update": after}))
        result = await service.update_file(store.user_id, saved.id, FileUpdate(description="Same target"))
        assert result.file.description == "Same target" and afters == []
