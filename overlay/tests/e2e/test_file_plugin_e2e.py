"""The same File contracts against native PostgreSQL."""
import pytest
from tests.file_plugin_cases import FilePluginCases, make_store

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture
async def file_store(db_adapter):
    return await make_store(db_adapter, "Postgres")


class TestFilePluginPostgres(FilePluginCases):
    pass
