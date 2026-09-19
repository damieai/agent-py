from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from agent_py.config import get_settings
from agent_py.db import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
url = get_settings().database_url

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            if connection.dialect.name == "sqlite":
                # SQLite batch rebuilds drop referenced tables. Use one explicit maintenance
                # transaction, then validate every reference before committing any DDL/data.
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                connection.commit()
                try:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    context.configure(connection=connection, target_metadata=Base.metadata)
                    with context.begin_transaction():
                        context.run_migrations()
                    if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                        raise RuntimeError("Migration left invalid foreign key references")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            else:
                context.configure(connection=connection, target_metadata=Base.metadata)
                with context.begin_transaction():
                    context.run_migrations()
    finally:
        engine.dispose()
