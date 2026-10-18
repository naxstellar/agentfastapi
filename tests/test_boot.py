from sqlalchemy import inspect

import app.main
import app.redis_client
from tests.conftest import make_schema_engine

AGENT_TABLES = {
    "conversations",
    "messages",
    "runs",
    "datasets",
    "conversation_datasets",
    "artifacts",
    "token_usage_daily",
    "user_settings",
}


async def test_lifespan_creates_all_tables(fresh_schema, async_redis_db, monkeypatch):
    engine = make_schema_engine(fresh_schema)
    monkeypatch.setattr(app.main, "engine", engine)
    monkeypatch.setattr(app.main, "async_redis_client", async_redis_db)
    try:
        async with app.main.lifespan(app.main.app):
            tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert AGENT_TABLES | {"users"} <= tables


def test_run_and_dataset_status_are_constrained(db_engine):
    inspector = inspect(db_engine)
    assert "ck_runs_status" in {c["name"] for c in inspector.get_check_constraints("runs")}
    assert "ck_datasets_status" in {c["name"] for c in inspector.get_check_constraints("datasets")}


async def test_async_redis_client_is_usable(async_redis_db):
    await app.redis_client.async_redis_client.set("k", "v")
    assert await app.redis_client.async_redis_client.get("k") == "v"
