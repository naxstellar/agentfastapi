from alembic import context
from sqlalchemy import create_engine

import app.models  # noqa: F401 - registers the tables with Base.metadata
import app.models_agent  # noqa: F401
from app.config import DATABASE_URL
from app.db import Base

target_metadata = Base.metadata

# LangGraph's checkpointer/store tables are created by their own setup() (app.migrate calls it), not by us.
LANGGRAPH_TABLES = {
    "checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations", "store", "store_migrations",
    "store_vectors", "vector_migrations",
}


def include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "table" and reflected and name in LANGGRAPH_TABLES)


def run_migrations_online() -> None:
    # Tests hand in a connection (bound to a throwaway schema); otherwise connect to DATABASE_URL.
    given = context.config.attributes.get("connection")
    if given is not None:
        _migrate(given)
        return
    with create_engine(DATABASE_URL).connect() as connection:
        _migrate(connection)


def _migrate(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
    connection.commit()


run_migrations_online()
