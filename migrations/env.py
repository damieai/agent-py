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
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
