"""Real adapter/migration checks; run only inside the reviewed Docker packet."""
import asyncio
import base64
import importlib
import json
import shutil
import sys
from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import inspect, text

from alembic import command
from app.config.settings import settings
from app.models.skill_bundle_models import SkillBundle
from app.services.skill_service import SkillService

HEAD = "20261111_skill_bundle_merge"
STARTS = [None, "20261008_plan_external_ref", "20261103_memory_search_tsv",
          "20261107_skill_bundle_resources"]
ROOT = Path.cwd()


class FixedEmbeddings:
    async def generate_embedding(self, text):
        return [1.0] + [0.0] * (settings.EMBEDDING_DIMENSIONS - 1)


async def one_path(backend, number, start):
    settings.DATABASE = backend
    settings.SKILLS_ENABLED = True
    settings.FILES_ENABLED = True
    if backend == "Postgres":
        import asyncpg

        admin = await asyncpg.connect(
            host=settings.POSTGRES_HOST, port=settings.PGPORT,
            user=settings.POSTGRES_USER, password=settings.POSTGRES_PASSWORD,
            database="forgetful",
        )
        name = "migration_upgrade_194_" + str(number)
        try:
            # Fixed names in the packet-owned disposable PostgreSQL instance.
            await admin.execute('CREATE DATABASE "' + name + '"')
        finally:
            await admin.close()
        settings.POSTGRES_DB = name
        module = "postgres"
        adapter_name = "PostgresDatabaseAdapter"
        repository_name = "PostgresSkillRepository"
    else:
        settings.SQLITE_MEMORY = False
        settings.SQLITE_PATH = "/work/upgrade-" + str(number) + ".db"
        assert not Path(settings.SQLITE_PATH).exists()
        module = "sqlite"
        adapter_name = "SqliteDatabaseAdapter"
        repository_name = "SqliteSkillRepository"
    prefix = "app.repositories." + module + "."
    adapter = getattr(importlib.import_module(prefix + module + "_adapter"), adapter_name)()
    tables = importlib.import_module(prefix + module + "_tables")
    repository = getattr(importlib.import_module(prefix + "skill_repository"), repository_name)
    service = SkillService(repository(adapter, FixedEmbeddings()))
    config = Config(str(ROOT / "alembic.ini"))
    owner = uuid4()
    skill_id = None
    before_bundle = None
    old_memory = None
    try:
        if start is not None:
            async with adapter._engine.begin() as conn:
                if backend == "Postgres":
                    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

                def prepare(sync_conn):
                    config.attributes["connection"] = sync_conn
                    command.upgrade(config, start)
                    return sorted(inspect(sync_conn).get_table_names())

                starting_tables = await conn.run_sync(prepare)
                if backend == "SQLite" and start == STARTS[3]:
                    await conn.execute(text(
                        "CREATE VIRTUAL TABLE vec_skills USING vec0("
                        "skill_id TEXT PRIMARY KEY, embedding FLOAT["
                        + str(settings.EMBEDDING_DIMENSIONS) + "])"))
            async with adapter.system_session() as session:
                session.add(tables.UsersTable(
                    id=owner if backend == "Postgres" else str(owner),
                    external_id="migration-owner", name="Synthetic owner",
                    email="migration@example.test",
                ))
                await session.flush()
                values = dict(
                    user_id=owner if backend == "Postgres" else str(owner),
                    title="migrationneedle", content="Preserved café memory",
                    context="Synthetic migration fixture", keywords=["migrationneedle"],
                    tags=["upgrade"], importance=7,
                )
                if backend == "Postgres":
                    values["embedding"] = await FixedEmbeddings().generate_embedding("")
                memory = tables.MemoryTable(**values)
                session.add(memory)
                await session.flush()
                old_memory = memory.id
            if start == STARTS[3]:
                bundle = SkillBundle.model_validate({
                    "schema_version": 1,
                    "skill_md": "---\nname: migration-package\ndescription: Preserve a package\n---\n\nUse [asset](assets/item.bin).",
                    "resources": [{"kind": "file", "path": "assets/item.bin",
                                   "description": "Preserved binary", "data": "AP9hc3NldA==",
                                   "mime_type": "application/octet-stream"}],
                })
                created = await service.import_skill_bundle(owner, bundle)
                skill_id = created.skill.id
                before_bundle = (await service.export_skill_bundle(
                    owner, skill_id, allowed_kinds=frozenset({"file"}),
                )).model_dump(mode="json")
        else:
            starting_tables = []
        await adapter.init_db()
        await adapter.init_db()
        async with adapter._engine.connect() as conn:
            assert (await conn.execute(text(
                "SELECT version_num FROM alembic_version"))).scalars().all() == [HEAD]
            ending_tables = await conn.run_sync(lambda c: sorted(inspect(c).get_table_names()))
            assert "skill_bundle_resources" in ending_tables
            if old_memory is not None:
                assert (await conn.execute(text(
                    "SELECT content FROM memories WHERE id = :id"), {"id": old_memory},
                )).scalar_one() == "Preserved café memory"
            if backend == "SQLite":
                names = set((await conn.execute(text(
                    "SELECT name FROM sqlite_master"))).scalars())
                assert {"memories_fts", "memories_fts_src", "memories_fts_ai",
                        "memories_fts_ad", "memories_fts_bu", "memories_fts_au"} <= names
                if old_memory is not None:
                    assert (await conn.execute(text(
                        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'migrationneedle'",
                    ))).scalars().all() == [old_memory]
            else:
                assert (await conn.execute(text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_name='memories' "
                    "AND column_name='search_tsv'"))).scalar_one() == 1
                assert (await conn.execute(text(
                    "SELECT count(*) FROM pg_trigger WHERE tgname='memories_search_tsv_trg' "
                    "AND NOT tgisinternal"))).scalar_one() == 1
                assert (await conn.execute(text(
                    "SELECT count(*) FROM pg_indexes WHERE tablename='memories' "
                    "AND indexname='ix_memories_search_tsv' AND indexdef LIKE '%USING gin%'",
                ))).scalar_one() == 1
                if old_memory is not None:
                    assert (await conn.execute(text(
                        "SELECT search_tsv @@ to_tsquery('simple', 'migrationneedle') "
                        "FROM memories WHERE id=:id"), {"id": old_memory},
                    )).scalar_one() is True
        if before_bundle is not None:
            after = await service.export_skill_bundle(
                owner, skill_id, allowed_kinds=frozenset({"file"}),
            )
            assert after.model_dump(mode="json") == before_bundle
        if start is None:
            async with adapter.system_session() as session:
                session.add(tables.UsersTable(
                    id=owner if backend == "Postgres" else str(owner),
                    external_id="migration-owner", name="Synthetic owner",
                    email="migration@example.test",
                ))
        new_bundle = SkillBundle.model_validate({
            "schema_version": 1,
            "skill_md": "---\nname: post-upgrade-package\ndescription: Use the upgraded schema\n---\n\nRead [reference](references/guide.md) and [asset](assets/item.bin).",
            "resources": [
                {"kind": "file", "path": "assets/item.bin", "description": "Binary",
                 "data": "AP9hc3NldA==", "mime_type": "application/octet-stream"},
                {"kind": "document", "path": "references/guide.md", "title": "Guide",
                 "description": "Reference", "content": "Retained café reference",
                 "document_type": "markdown"},
            ],
        })
        imported = await service.import_skill_bundle(owner, new_bundle)
        allowed = frozenset({"file", "document"})
        exported = await service.export_skill_bundle(owner, imported.skill.id, allowed_kinds=allowed)
        again = await service.export_skill_bundle(owner, imported.skill.id, allowed_kinds=allowed)
        assert again.model_dump(mode="json") == exported.model_dump(mode="json")
        resources = {resource.path: resource for resource in exported.resources}
        assert set(resources) == {"assets/item.bin", "references/guide.md"}
        assert base64.b64decode(resources["assets/item.bin"].data, validate=True) == b"\x00\xffasset"
        assert resources["references/guide.md"].content == "Retained café reference"
        return dict(start=start, backend=backend, status="PASS", real_migrations=True,
                    repeated_adapter_startup=True, memory_seeded=old_memory is not None,
                    prior_bundle_export_identical=before_bundle is not None,
                    post_upgrade_file_document_roundtrip=True, repeated_export_identical=True,
                    synthetic_vec_skills_setup=backend == "SQLite" and start == STARTS[3],
                    initial_tables=starting_tables, final_tables=ending_tables)
    finally:
        await adapter.dispose()


async def main():
    backend = sys.argv[1]
    assert backend in {"SQLite", "Postgres"}
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]
    if backend == "SQLite":
        # Actual Alembic graph/upgrade negative control, without the new join.
        old_scripts = Path("/work/unjoined-migrations")
        shutil.copytree(ROOT / "alembic", old_scripts,
                        ignore=shutil.ignore_patterns("20261111_merge_skill_bundle_memory_search.py"))
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(old_scripts))
        assert set(ScriptDirectory.from_config(config).get_heads()) == set(STARTS[2:])
        from sqlalchemy import create_engine

        settings.DATABASE = "SQLite"
        engine = create_engine("sqlite:////work/unjoined.db")
        try:
            with engine.begin() as connection:
                config.attributes["connection"] = connection
                try:
                    command.upgrade(config, "head")
                except CommandError as error:
                    assert "Multiple head revisions" in str(error), str(error)
                else:
                    raise AssertionError("Unjoined migration graph unexpectedly upgraded")
        finally:
            engine.dispose()
    results = [await one_path(backend, i, start) for i, start in enumerate(STARTS)]
    if backend == "SQLite":
        assert results[1]["initial_tables"] == results[2]["initial_tables"]
    Path("/runtime/migration-upgrades-" + backend + ".json").write_text(
        json.dumps(dict(status="PASS", paths=results), indent=2))


asyncio.run(main())
