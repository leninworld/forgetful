"""Skill policy workflows against real, isolated SQLite persistence."""

import asyncio
import os
from uuid import uuid4

import pytest

from app.config.settings import settings
from app.exceptions import WritePluginRejectedError
from app.models.skill_models import SkillCreate, SkillUpdate
from app.models.user_models import UserCreate
from app.models.write_plugin_models import PluginWarning, WriteDecision
from app.protocols.skill_plugin_protocol import SkillPlugin
from app.repositories.sqlite.skill_repository import SqliteSkillRepository
from app.repositories.sqlite.sqlite_adapter import SqliteDatabaseAdapter
from app.repositories.sqlite.user_repository import SqliteUserRepository
from app.services.skill_plugin_service import SkillPluginService
from app.services.skill_service import SkillService
from app.services.user_service import UserService


class RecordingEmbeddings:
    def __init__(self):
        self.requests = []

    async def generate_embedding(self, text):
        self.requests.append(text)
        return [1.0] + [0.0] * (settings.EMBEDDING_DIMENSIONS - 1)


def candidate(**changes):
    return SkillCreate(**{
        "name": "safe-skill", "description": "Synthetic workflow",
        "content": "Use the documented tool.", "metadata": {"nested": {"safe": True}},
        **changes,
    })


@pytest.fixture(params=["SQLite", pytest.param("Postgres", marks=pytest.mark.e2e)])
async def skill_store(sqlite_runtime_settings, monkeypatch, request):
    embeddings = RecordingEmbeddings()
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    if request.param == "Postgres":
        host = os.environ.get("FORGETFUL_TEST_PLUGIN_PG_HOST")
        if not host:
            pytest.skip("Set FORGETFUL_TEST_PLUGIN_PG_HOST for an isolated disposable PostgreSQL database")
        from app.repositories.postgres.postgres_adapter import PostgresDatabaseAdapter
        from app.repositories.postgres.skill_repository import PostgresSkillRepository
        from app.repositories.postgres.user_repository import PostgresUserRepository

        monkeypatch.setattr(settings, "DATABASE", "Postgres")
        monkeypatch.setattr(settings, "POSTGRES_HOST", host)
        monkeypatch.setattr(settings, "PGPORT", int(os.environ.get("FORGETFUL_TEST_PLUGIN_PG_PORT", "5432")))
        monkeypatch.setattr(settings, "POSTGRES_DB", "forgetful_skill_plugin_test")
        monkeypatch.setattr(settings, "POSTGRES_USER", "forgetful_skill_plugin_test")
        monkeypatch.setattr(settings, "POSTGRES_PASSWORD", "synthetic-test-password")
        db = PostgresDatabaseAdapter()
        repo = PostgresSkillRepository(db, embeddings)
        users = UserService(PostgresUserRepository(db))
    else:
        db = SqliteDatabaseAdapter()
        repo = SqliteSkillRepository(db, embeddings)
        users = UserService(SqliteUserRepository(db))
    await db.init_db()
    suffix = str(uuid4())
    owner = await users.get_or_create_user(UserCreate(
        external_id="skill-policy-owner-" + suffix, name="Owner", email="owner@example.com",
    ))
    other = await users.get_or_create_user(UserCreate(
        external_id="skill-policy-other-" + suffix, name="Other", email="other@example.com",
    ))
    try:
        yield repo, owner.id, other.id, embeddings
    finally:
        await db.dispose()


def service(repo, before=None, after=None, mode="enforce", timeout=5.0):
    plugin = SkillPlugin(before_save=before or {}, after_save=after or {})
    return SkillService(repo, write_plugin=SkillPluginService(
        plugin, mode=mode, timeout_seconds=timeout,
    ))


async def test_reject_prevents_embedding_and_persistence(skill_store):
    repo, owner, _, embeddings = skill_store

    async def reject(request):
        assert request.operation == "create"
        assert request.user_id == owner
        assert request.previous is None
        return WriteDecision(outcome="reject", message="Skill policy rejected this content.")

    with pytest.raises(WritePluginRejectedError, match="policy rejected"):
        await service(repo, before={"create": reject}).create_skill(owner, candidate())
    assert embeddings.requests == []
    assert not await repo.skill_name_exists(owner, "safe-skill")


async def test_warn_mode_persists_and_returns_only_transient_warning(skill_store):
    repo, owner, _, _ = skill_store

    async def reject(request):
        return WriteDecision(outcome="reject", message="Review these instructions.")

    result = await service(repo, before={"create": reject}, mode="warn").create_skill(
        owner, candidate(),
    )
    assert result.plugin_warnings[0].message == "Review these instructions."
    stored = await repo.get_skill_by_id(owner, result.skill.id)
    assert stored.content == "Use the documented tool."
    assert "plugin_warnings" not in stored.model_dump()


async def test_handler_snapshots_cannot_rewrite_nested_input_or_saved_data(skill_store):
    repo, owner, _, _ = skill_store
    submitted = candidate()

    async def before(request):
        request.submitted.content = "Attempted submitted rewrite"
        request.proposed.metadata["nested"]["safe"] = False
        request.proposed.content = "Attempted proposed rewrite"
        return WriteDecision(outcome="allow")

    async def after(request):
        request.saved.content = "Attempted saved rewrite"
        request.saved.metadata["nested"]["safe"] = False
        return PluginWarning(message="Stored.")

    result = await service(repo, before={"create": before}, after={"create": after}).create_skill(
        owner, submitted,
    )
    stored = await repo.get_skill_by_id(owner, result.skill.id)
    assert submitted.content == stored.content == result.skill.content == "Use the documented tool."
    assert submitted.metadata == stored.metadata == {"nested": {"safe": True}}
    assert result.plugin_warnings[0].message == "Stored."


@pytest.mark.parametrize("failure", ["exception", "malformed", "timeout", "child-cancel"])
async def test_after_failure_preserves_confirmed_save_and_sanitizes_warning(skill_store, failure):
    repo, owner, _, _ = skill_store

    async def after(request):
        if failure == "exception":
            raise RuntimeError("synthetic-secret-do-not-expose")
        if failure == "malformed":
            return {"outcome": "reject", "message": "synthetic-secret-do-not-expose"}
        if failure == "timeout":
            await asyncio.Event().wait()
        child = asyncio.create_task(asyncio.sleep(100))
        child.cancel()
        await child

    result = await service(repo, after={"create": after}, timeout=0.01).create_skill(
        owner, candidate(),
    )
    assert await repo.get_skill_by_id(owner, result.skill.id) is not None
    assert result.plugin_warnings
    assert "synthetic-secret" not in str(result.plugin_warnings)


async def test_import_uses_exactly_one_create_dispatch(skill_store):
    repo, owner, _, _ = skill_store
    calls = []

    async def before(request):
        calls.append(("before", request.operation))
        return WriteDecision(outcome="allow")

    async def after(request):
        calls.append(("after", request.operation))

    result = await service(repo, before={"create": before}, after={"create": after}).import_skill(
        owner,
        "---\nname: imported-skill\ndescription: Synthetic import\n---\nRead the tool guide.",
    )
    assert result.skill.name == "imported-skill"
    assert calls == [("before", "create"), ("after", "create")]


async def test_import_rejects_before_embedding_or_storage(skill_store):
    repo, owner, _, embeddings = skill_store

    async def reject(request):
        return WriteDecision(outcome="reject", message="Import denied.")

    with pytest.raises(WritePluginRejectedError, match="Import denied"):
        await service(repo, before={"create": reject}).import_skill(
            owner, "---\nname: denied-import\ndescription: Synthetic import\n---\nText.",
        )
    assert embeddings.requests == []
    assert not await repo.skill_name_exists(owner, "denied-import")


async def test_delete_reports_prior_snapshot_and_no_invented_saved_object(skill_store):
    repo, owner, _, _ = skill_store
    made = await SkillService(repo).create_skill(owner, candidate())
    calls = []

    async def before(request):
        calls.append(("before", request.operation))
        assert request.previous.id == made.skill.id
        return WriteDecision(outcome="allow")

    async def after(request):
        calls.append(("after", request.operation))
        assert request.previous.id == made.skill.id
        assert request.saved is None
        assert request.deleted is True
        assert await repo.get_skill_by_id(owner, request.previous.id) is None

    result = await service(repo, before={"delete": before}, after={"delete": after}).delete_skill(
        owner, made.skill.id,
    )
    assert result.success is True
    assert calls == [("before", "delete"), ("after", "delete")]


async def test_foreign_and_missing_delete_do_not_dispatch(skill_store):
    repo, owner, other, _ = skill_store
    made = await SkillService(repo).create_skill(owner, candidate())

    async def forbidden(request):
        pytest.fail("Missing or foreign skills must not reach a handler")

    configured = service(repo, before={"delete": forbidden}, after={"delete": forbidden})
    assert not (await configured.delete_skill(other, made.skill.id)).success
    assert not (await configured.delete_skill(owner, made.skill.id + 100)).success
    assert await repo.get_skill_by_id(owner, made.skill.id) is not None


@pytest.mark.parametrize("registered", ["create", "update"])
async def test_noop_update_does_not_dispatch(skill_store, monkeypatch, registered):
    from unittest.mock import AsyncMock
    repo, owner, _, embeddings = skill_store
    made = await SkillService(repo).create_skill(owner, candidate())
    before_calls = len(embeddings.requests)

    async def forbidden(request):
        pytest.fail("A no-op must not invoke a policy or advisory callback")

    update = AsyncMock(wraps=repo.update_skill)
    monkeypatch.setattr(repo, "update_skill", update)
    configured = service(repo, before={registered: forbidden}, after={registered: forbidden})
    result = await configured.update_skill(owner, made.skill.id, SkillUpdate(content=made.skill.content))
    update.assert_awaited_once()
    stored = await repo.get_skill_by_id(owner, made.skill.id)
    assert result.skill == stored
    assert result.skill.id == made.skill.id
    assert len(embeddings.requests) == before_calls


async def test_real_request_cancellation_during_after_propagates_without_rollback(skill_store):
    repo, owner, _, _ = skill_store
    entered = asyncio.Event()

    async def after(request):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(service(repo, after={"create": after}).create_skill(owner, candidate()))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await repo.skill_name_exists(owner, "safe-skill")


async def test_update_rechecks_after_handler_and_does_not_overwrite_unsubmitted_fields(skill_store):
    repo, owner, _, _ = skill_store
    made = await SkillService(repo).create_skill(owner, candidate())

    async def before(request):
        assert request.previous.content == "Use the documented tool."
        await repo.update_skill(owner, made.skill.id, SkillUpdate(description="Concurrent description"))
        request.proposed.description = "Handler must not persist this"
        return WriteDecision(outcome="allow")

    result = await service(repo, before={"update": before}).update_skill(
        owner, made.skill.id, SkillUpdate(content="New instructions"),
    )
    stored = await repo.get_skill_by_id(owner, made.skill.id)
    assert result.skill.content == stored.content == "New instructions"
    assert stored.description == "Concurrent description"


async def test_deleted_during_before_update_does_not_recreate_or_embed(skill_store):
    from app.exceptions import NotFoundError

    repo, owner, _, embeddings = skill_store
    made = await SkillService(repo).create_skill(owner, candidate())
    previous_calls = len(embeddings.requests)

    async def before(request):
        await repo.delete_skill(owner, made.skill.id)
        return WriteDecision(outcome="allow")

    with pytest.raises(NotFoundError):
        await service(repo, before={"update": before}).update_skill(
            owner, made.skill.id, SkillUpdate(description="No provider call"),
        )
    assert len(embeddings.requests) == previous_calls


async def test_unregistered_operations_do_not_dispatch(skill_store):
    repo, owner, _, _ = skill_store

    async def forbidden(request):
        pytest.fail("Only explicitly registered operations may dispatch")

    configured = service(repo, before={"delete": forbidden}, after={"delete": forbidden})
    made = await configured.create_skill(owner, candidate())
    changed = await configured.update_skill(owner, made.skill.id, SkillUpdate(content="Updated"))
    assert changed.skill.content == "Updated"
    assert made.plugin_warnings == changed.plugin_warnings == []


@pytest.mark.parametrize("mode", ["warn", "enforce"])
@pytest.mark.parametrize("failure", ["exception", "malformed", "timeout"])
async def test_before_failure_obeys_mode_without_exposing_payload(skill_store, mode, failure):
    from app.exceptions import WritePluginUnavailableError

    repo, owner, _, embeddings = skill_store

    async def before(request):
        if failure == "exception":
            raise RuntimeError("synthetic-secret")
        if failure == "malformed":
            return {"outcome": "allow", "unexpected": "synthetic-secret"}
        await asyncio.Event().wait()

    configured = service(repo, before={"create": before}, mode=mode, timeout=0.01)
    if mode == "enforce":
        with pytest.raises(WritePluginUnavailableError, match="Skill Write Plugin unavailable"):
            await configured.create_skill(owner, candidate())
        assert not await repo.skill_name_exists(owner, "safe-skill")
        assert embeddings.requests == []
    else:
        result = await configured.create_skill(owner, candidate())
        assert result.plugin_warnings
        assert "synthetic-secret" not in str(result.plugin_warnings)
        assert await repo.get_skill_by_id(owner, result.skill.id) is not None


async def test_skill_deletion_cleans_associations_without_deleting_linked_memory(skill_store):
    from sqlalchemy import text

    from app.models.memory_models import MemoryCreate

    repo, owner, _, embeddings = skill_store
    if settings.DATABASE == "Postgres":
        from app.repositories.postgres.memory_repository import PostgresMemoryRepository
        memories = PostgresMemoryRepository(repo.db_adapter, embeddings)
    else:
        from app.repositories.sqlite.memory_repository import SqliteMemoryRepository
        memories = SqliteMemoryRepository(repo.db_adapter, embeddings)
    made = await SkillService(repo).create_skill(owner, candidate())
    memory = await memories.create_memory(owner, MemoryCreate(
        title="Linked synthetic memory", content="A durable fact", context="Policy cleanup test",
        keywords=[], tags=[], skill_ids=[made.skill.id],
    ))
    calls = []

    async def after(request):
        calls.append(request.operation)

    async def count_links():
        async with repo.db_adapter.session(owner) as session:
            result = await session.execute(text(
                "SELECT COUNT(*) FROM memory_skill_association WHERE skill_id = :skill_id",
            ), {"skill_id": made.skill.id})
            return result.scalar_one()

    assert await count_links() == 1
    result = await service(repo, after={"delete": after}).delete_skill(owner, made.skill.id)
    assert result.success and calls == ["delete"]
    assert await count_links() == 0
    assert await memories.get_memory_by_id(owner, memory.id) is not None


async def test_after_context_preparation_error_is_advisory(skill_store, monkeypatch):
    from app.models.skill_models import Skill

    repo, owner, _, _ = skill_store

    async def after(request):
        pytest.fail("Unavailable detached context must not reach the handler")

    def fail_copy(self, **kwargs):
        raise RuntimeError("synthetic-secret")

    monkeypatch.setattr(Skill, "model_copy", fail_copy)
    result = await service(repo, after={"create": after}).create_skill(owner, candidate())
    assert result.plugin_warnings[0].code == "SKILL_PLUGIN_AFTER_SAVE_UNAVAILABLE"
    assert "synthetic-secret" not in str(result.plugin_warnings)
    assert await repo.skill_name_exists(owner, "safe-skill")


async def test_missing_delete_without_plugin_keeps_repository_call_and_warning(skill_store, monkeypatch, caplog):
    import logging
    from unittest.mock import AsyncMock

    from app.services.skill_service import logger

    repo, owner, other, _ = skill_store
    made = await SkillService(repo).create_skill(owner, candidate())
    delete = AsyncMock(wraps=repo.delete_skill)
    monkeypatch.setattr(repo, "delete_skill", delete)
    monkeypatch.setattr(logger, "disabled", False)
    caplog.set_level(logging.WARNING, logger=logger.name)
    configured = SkillService(repo)
    assert not (await configured.delete_skill(other, made.skill.id)).success
    assert not (await configured.delete_skill(owner, made.skill.id + 100)).success
    assert delete.await_count == 2
    assert sum(record.message == "skill not found for deletion" for record in caplog.records) == 2
    assert await repo.get_skill_by_id(owner, made.skill.id) is not None


@pytest.mark.parametrize("mode", ["warn", "enforce"])
async def test_uncomposable_update_context_obeys_unavailability_mode(skill_store, monkeypatch, mode):
    from unittest.mock import AsyncMock

    from app.exceptions import WritePluginUnavailableError

    repo, owner, _, embeddings = skill_store
    made = await SkillService(repo).create_skill(owner, candidate())
    original_read = repo.get_skill_by_id
    # Simulate an adapter returning an old, now-invalid value. The real stored row stays valid.
    legacy = made.skill.model_copy(update={"content": None})
    monkeypatch.setattr(repo, "get_skill_by_id", AsyncMock(return_value=legacy))
    update = AsyncMock(wraps=repo.update_skill)
    monkeypatch.setattr(repo, "update_skill", update)
    previous_embeddings = len(embeddings.requests)

    async def forbidden(request):
        pytest.fail("An uncomposable context must not reach the handler")

    configured = service(repo, before={"update": forbidden}, mode=mode)
    if mode == "enforce":
        with pytest.raises(WritePluginUnavailableError, match="Skill Write Plugin unavailable"):
            await configured.update_skill(owner, made.skill.id, SkillUpdate(description="Updated description"))
        update.assert_not_awaited()
        assert len(embeddings.requests) == previous_embeddings
    else:
        result = await configured.update_skill(
            owner, made.skill.id, SkillUpdate(description="Updated description"),
        )
        update.assert_awaited_once()
        assert result.plugin_warnings[0].code == "SKILL_WRITE_PLUGIN_UNAVAILABLE"
        assert result.skill.description == "Updated description"
        assert result.skill.content == "Use the documented tool."
    assert (await original_read(owner, made.skill.id)).content == "Use the documented tool."
