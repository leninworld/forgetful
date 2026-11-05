"""Exercise installed File public classes with real SQLite, no source import."""
import asyncio
import json
from pathlib import Path

from app.config.settings import settings
from app.models.file_models import FileCreate, FileUpdate
from app.models.file_plugin_models import FileMetadata
from app.models.user_models import UserCreate
from app.models.write_plugin_models import WriteDecision
from app.protocols.file_plugin_protocol import FilePlugin
from app.repositories.sqlite.file_repository import SqliteFileRepository
from app.repositories.sqlite.project_repository import SqliteProjectRepository
from app.repositories.sqlite.sqlite_adapter import SqliteDatabaseAdapter
from app.repositories.sqlite.user_repository import SqliteUserRepository
from app.services.file_service import FileService
from app.services.file_write_plugin_service import FileWritePluginService
from app.services.user_service import UserService
from app.exceptions import WritePluginRejectedError
import app.services.file_service as installed


async def main():
    assert Path(installed.__file__).resolve().is_relative_to(Path("/work/installed"))
    settings.DATABASE = "SQLite"
    settings.SQLITE_MEMORY = True
    calls = []
    async def before(ctx):
        calls.append(ctx.model_copy(deep=True))
        if ctx.proposed and ctx.proposed.filename == "blocked.bin":
            return WriteDecision(outcome="reject", message="Synthetic rule")
        if ctx.operation == "create":
            assert ctx.content == b"abc"
        return WriteDecision(outcome="warn", message="Synthetic review")
    async def after(ctx):
        calls.append(ctx.model_copy(deep=True))
    plugin = FilePlugin(before_save={op: before for op in ("create", "update", "delete")},
                        after_save={op: after for op in ("create", "update", "delete")})
    db = SqliteDatabaseAdapter()
    await db.init_db()
    try:
        user = await UserService(SqliteUserRepository(db)).get_or_create_user(UserCreate(
            external_id="installed-file-demo", name="Synthetic demo", email="file@example.test",
        ))
        service = FileService(SqliteFileRepository(db), file_plugin=FileWritePluginService(plugin),
                              project_repo=SqliteProjectRepository(db))
        data = FileCreate(filename="allowed.bin", description="Installed API", mime_type="text/plain", data="YWJj")
        made = await service.create_file(user.id, data)
        assert made.file.size_bytes == 3 and made.plugin_warnings
        assert isinstance(calls[1].saved, FileMetadata) and "data" not in calls[1].saved.model_dump()
        try:
            await service.create_file(user.id, data.model_copy(update={"filename": "blocked.bin"}))
        except WritePluginRejectedError:
            pass
        else:
            raise AssertionError("Installed policy failed to reject")
        changed = await service.update_file(user.id, made.file.id, FileUpdate(data="eHl6"))
        assert changed.file.data == "eHl6" and "data" in calls[-1].changed_fields
        removed = await service.delete_file(user.id, made.file.id)
        assert removed.deleted and calls[-1].deleted_file_id == made.file.id
        print(json.dumps({"installed_File_API": "PASS", "create_warn": True, "reject": True,
                          "content_replace": True, "confirmed_delete": True, "binary_context_bounded": True}))
    finally:
        await db.dispose()


asyncio.run(main())
