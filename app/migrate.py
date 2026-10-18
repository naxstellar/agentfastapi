"""Schema setup for deployed environments: `python -m app.migrate` (the Kubernetes `migrate` Job).

Runs `alembic upgrade head`, then LangGraph's checkpointer/store setup() (idempotent). The api and workers don't
create tables outside APP_ENV=local, so this must run before each rollout.
"""
import asyncio
import logging
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.agent.graph import open_persistence
from app.config import DATABASE_URL
from app.observability import configure_logging

logger = logging.getLogger("app.migrate")
ROOT = Path(__file__).resolve().parent.parent


BASELINE = "0001"


def upgrade_schema() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        stamped = "alembic_version" in tables and conn.execute(text("SELECT count(*) FROM alembic_version")).scalar() > 0
    engine.dispose()
    if "users" in tables and not stamped:
        # A database made by create_all (APP_ENV=local): adopt it at the baseline instead of re-creating tables.
        logger.info("existing schema without a stamped revision: stamping %s", BASELINE)
        command.stamp(cfg, BASELINE)
    command.upgrade(cfg, "head")


async def setup_langgraph() -> None:
    async with open_persistence():
        pass


def main() -> None:
    configure_logging()
    logger.info("alembic upgrade head")
    upgrade_schema()
    logger.info("langgraph setup")
    if sys.platform == "win32":  # psycopg's async mode needs a selector loop
        asyncio.run(setup_langgraph(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(setup_langgraph())
    logger.info("done")


if __name__ == "__main__":
    main()
