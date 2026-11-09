"""Task policy contracts against real file-backed SQLite."""
import pytest

from app.config.settings import settings
from app.repositories.sqlite.sqlite_adapter import SqliteDatabaseAdapter
from tests.plan_plugin_cases import plan_store as plan_store
from tests.task_plugin_cases import TestTaskPluginPersistence as TestTaskPluginPersistence
from tests.task_plugin_cases import task_store as task_store

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def reference_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE", "SQLite")
    monkeypatch.setattr(settings, "SQLITE_MEMORY", False)
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "task-policy.db"))
    adapter = SqliteDatabaseAdapter()
    await adapter.init_db()
    try:
        yield adapter
    finally:
        await adapter.dispose()
