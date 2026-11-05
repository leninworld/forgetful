"""File hooks with real isolated SQLite persistence."""
import pytest
from tests.file_plugin_cases import FilePluginCases, make_store


@pytest.fixture
async def file_store(sqlite_runtime_settings):
    from app.repositories.sqlite.sqlite_adapter import SqliteDatabaseAdapter
    db = SqliteDatabaseAdapter()
    await db.init_db()
    try:
        yield await make_store(db, "SQLite")
    finally:
        await db.dispose()


class TestFilePluginSQLite(FilePluginCases):
    pass
