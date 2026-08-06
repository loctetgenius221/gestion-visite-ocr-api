from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import settings

# `app.models` réexporte tous les modèles : indispensable pour que `target_metadata`
# soit complet et que l'autogénération détecte l'ensemble des tables.
from app.models import Base

config = context.config

# L'URL vient de l'environnement applicatif, jamais du fichier .ini (spec §7) —
# sauf si l'appelant en a explicitement fourni une (tests de migration, scripts).
_configured_url = config.get_main_option("sqlalchemy.url", "")
if not _configured_url or _configured_url.startswith("driver://"):
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)


def database_url() -> str:
    return config.get_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Migrations en mode offline : génère le SQL sans connexion à la base."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # Nécessaire sur SQLite : les ALTER TABLE y sont trop limités sans batch mode.
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
