import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

BACKEND = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def _db_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(autouse=True, scope="session")
def _private_rate_slots(tmp_path_factory):
    """Tests must not share SEC rate slots with real downloads running on the same machine."""
    os.environ["CATALYSTEDGE_RATE_DIR"] = str(tmp_path_factory.mktemp("rate"))


@pytest.fixture(scope="session")
def engine():
    url = _db_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL not set (DB tests need Postgres)")
    eng = create_engine(url, future=True)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env, check=True,
                   capture_output=True)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """Each test runs inside a transaction that is rolled back afterwards."""
    conn = engine.connect()
    trans = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()
