"""Original-main compatible native PostgreSQL oracle and observations."""
import pytest
from tests.file_plugin_baseline_cases import FileBaselineCases, make_baseline_store

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture
async def baseline_store(db_adapter):
    return await make_baseline_store(db_adapter, "Postgres")


class TestFileBaselinePostgres(FileBaselineCases):
    pass
