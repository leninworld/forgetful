"""Original-main compatible SQLite feature oracle and observations."""
import pytest
from tests.file_plugin_baseline_cases import FileBaselineCases, make_baseline_store


@pytest.fixture
async def baseline_store(sqlite_runtime_settings):
    from app.repositories.sqlite.sqlite_adapter import SqliteDatabaseAdapter
    db = SqliteDatabaseAdapter()
    await db.init_db()
    try:
        yield await make_baseline_store(db, "SQLite")
    finally:
        await db.dispose()


class TestFileBaselineSQLite(FileBaselineCases):
    pass
