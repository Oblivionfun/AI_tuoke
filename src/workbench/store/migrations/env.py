from alembic import context
from sqlalchemy import create_engine

from workbench.config import get_settings
from workbench.store.models import Base

target_metadata = Base.metadata


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    url = context.config.attributes.get("connection_url") or get_settings().database_url
    with create_engine(url).connect() as conn:
        _run(conn)
        conn.commit()
