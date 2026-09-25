"""Alembic environment. The URL comes from DATABASE_URL_DIRECT (Neon direct, for
migrations) or DATABASE_URL; never from alembic.ini."""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from catalystedge.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    url = os.environ.get("DATABASE_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("set DATABASE_URL (or DATABASE_URL_DIRECT) to run migrations")
    from catalystedge.db.session import normalize_url

    return normalize_url(url)


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
