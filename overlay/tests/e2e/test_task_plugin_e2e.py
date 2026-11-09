"""The identical Task Plugin contracts against native PostgreSQL."""
import pytest

from tests.plan_plugin_cases import plan_store as plan_store
from tests.task_plugin_cases import TestTaskPluginPersistence as TestTaskPluginPersistence
from tests.task_plugin_cases import task_store as task_store

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture
def reference_db(db_adapter):
    return db_adapter
