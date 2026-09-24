"""Engine/session factory.

In the cloud profile the database is Neon, which only scales to zero when no
connection is held open, so we use NullPool there (short-lived connections).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from catalystedge.config import get_settings


def make_engine(url: str | None = None, *, null_pool: bool | None = None) -> Engine:
    settings = get_settings()
    url = url or settings.database_url
    if null_pool is None:
        null_pool = settings.profile == "cloud"
    kwargs = {"poolclass": NullPool} if null_pool else {"pool_pre_ping": True, "pool_size": 5}
    return create_engine(url, future=True, **kwargs)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
