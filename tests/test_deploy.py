"""WP10: health endpoints and the Alembic baseline."""
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

import app.main
from tests.conftest import ROOT_DIR, make_schema_engine


async def test_healthz_needs_no_dependencies(client, monkeypatch):
    monkeypatch.setattr(app.main, "engine", None)  # would raise if touched
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_ok_reports_version(client, db_engine, monkeypatch):
    monkeypatch.setattr(app.main, "engine", db_engine)
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": app.main.APP_VERSION}


async def test_readyz_503_when_postgres_down(client, monkeypatch):
    class Broken:
        def connect(self):
            raise RuntimeError("secret connection string")

    monkeypatch.setattr(app.main, "engine", Broken())
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["failed"] == ["postgres"]
    assert "secret" not in response.text


async def test_readyz_503_when_redis_down(client, db_engine, monkeypatch):
    class Broken:
        def ping(self):
            raise RuntimeError("down")

    monkeypatch.setattr(app.main, "engine", db_engine)
    monkeypatch.setattr(app.main, "redis_client", Broken())
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["failed"] == ["redis"]


def test_alembic_upgrade_on_empty_schema_matches_models(fresh_schema):
    engine = make_schema_engine(fresh_schema)
    cfg = Config(str(ROOT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT_DIR / "migrations"))
    with engine.connect() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
        assert {"users", "conversations", "runs", "alembic_version"} <= set(inspect(connection).get_table_names())
        command.check(cfg)  # raises if the models and the migrations drifted apart
    engine.dispose()
