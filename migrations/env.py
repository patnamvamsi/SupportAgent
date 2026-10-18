"""Alembic environment (03 §4).

Online migrations run against the connection supplied by
``bootstrap.run_migrations`` (the URL comes from the config file + the
``SUPPORTAGENT_DB_URL`` environment variable — never from a checked-in file).
Offline mode renders the script to stdout for review (``--sql``).

``target_metadata`` is the single DDL source in
``supportagent.adapters.db.tables`` (03 §4: one definition, two dialects).
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from supportagent.adapters.db.tables import metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def run_migrations_offline() -> None:
    """Render the migration script for ``--sql`` (no live connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run the migration script against a live connection (03 §4)."""
    connectable = config.get_main_option("sqlalchemy.url")
    if connectable:
        engine = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
    else:  # pragma: no cover - defensive; bootstrap always supplies a URL
        raise RuntimeError(
            "migrations: no sqlalchemy.url supplied — run via supportagent migrate"
        )
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
