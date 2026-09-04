"""Alembic environment.

The database URL comes from `Settings`, not from `alembic.ini`. One config
surface for the whole platform means a migration can never run against a
different database than the application — which is exactly the failure that makes
people distrust migrations and reach for `create_all()`.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from recoveryai.core.settings import get_settings  # noqa: E402
from recoveryai.db.models import Base  # noqa: E402

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().resolved_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

#: Tables in the database that Alembic must not manage.
#:
#: `llm_daily_budget` is created and written by raw `sqlite3` in
#: `llm.governance.DailyBudgetCounter`, deliberately outside the ORM so the spend
#: counter stays readable while a transaction is in flight. Autogenerate has no
#: way to know that: it sees a table absent from `Base.metadata` and proposes
#: dropping it, which would delete the record of how much budget has been spent
#: today. Excluding it here makes `alembic check` tell the truth instead of
#: reporting drift that must never be acted on.
UNMANAGED_TABLES = frozenset({"llm_daily_budget"})


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001, ARG001
    if type_ == "table" and name in UNMANAGED_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most columns; batch mode rebuilds the table
            # instead, so future migrations are not blocked by the dialect.
            render_as_batch=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
