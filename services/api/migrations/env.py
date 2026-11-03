"""محیط Alembic (async) — آدرس دیتابیس از XAGENT_DATABASE_URL خوانده می‌شود."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from xagent_core import models  # noqa: F401  (ثبت همه جداول)
from xagent_core.config import get_settings
from xagent_core.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# ایندکس‌هایی که با SQL خام در Migration ساخته می‌شوند (HNSW و trigram)
MANUAL_INDEXES = {
    "ix_experiences_embedding",
    "ix_semantic_embedding",
    "ix_contacts_name_trgm",
    "ix_tasks_title_trgm",
    "ix_conversations_title_trgm",
}


def include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "index" and name in MANUAL_INDEXES)


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=True, include_object=include_object
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
