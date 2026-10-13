import os

import pytest
import sqlalchemy as sa

from molseal.store import SqlStore, metadata


@pytest.fixture(scope="session")
def pg_engine():
    url = os.environ.get("MOLSEAL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("MOLSEAL_TEST_DATABASE_URL not set")
    engine = sa.create_engine(url)
    metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_store(pg_engine):
    return SqlStore(pg_engine)
