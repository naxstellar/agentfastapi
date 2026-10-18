import asyncio
import json
import os
import sys
import uuid
from base64 import b64encode
from pathlib import Path
from urllib.parse import urlparse

import itsdangerous
import pytest
from dotenv import dotenv_values

# Must run before any `app.*` import: app/config.py reads the environment at import time and
# load_dotenv never overrides variables that are already set, so these dummies win over env/.env.
_DOTENV = dotenv_values(Path(__file__).resolve().parent.parent / "env" / ".env")
for _key, _value in {
    "AUTH0_DOMAIN": "test-tenant.example.auth0.com",
    "AUTH0_CLIENT_ID": "test-client-id",
    "AUTH0_CLIENT_SECRET": "test-client-secret",
    "AUTH0_AUDIENCE": "https://test-audience/",
    "AUTH0_CALLBACK_URL": "https://testserver/callback",
    "APP_BASE_URL": "https://testserver",
    "SESSION_SECRET_KEY": "test-session-secret",
}.items():
    os.environ.setdefault(_key, _value)
# Forced (not setdefault): a REDIS_URL exported in the shell (start.ps1 pins DB 0) must never reach the tests.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL") or "redis://127.0.0.1:6379/15"
os.environ["REDIS_URL"] = TEST_REDIS_URL
_REDIS_DB = int(urlparse(TEST_REDIS_URL).path.lstrip("/") or 0)
if _REDIS_DB in (0, 1, 2):  # dev sessions/state, Celery broker, event streams
    pytest.exit(f"TEST_REDIS_URL uses DB {_REDIS_DB}, which the dev app uses; pick another DB (default 15).", returncode=2)
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL")
    or _DOTENV.get("DATABASE_URL")
    or "postgresql+psycopg2://agentfastapi:agentfastapi@localhost:5432/agentfastapi",
)

import httpx  # noqa: E402
import redis  # noqa: E402
import redis.asyncio  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import app.auth.redis_state  # noqa: E402
import app.auth.session_store  # noqa: E402
import app.main  # noqa: E402
import app.redis_client  # noqa: E402
import app.routers.conversations  # noqa: E402
import app.routers.runs  # noqa: E402
import app.routers.usage  # noqa: E402
import app.workers.datasets  # noqa: E402
from app.auth.security import get_current_user  # noqa: E402
from app.config import DATABASE_URL, SESSION_SECRET_KEY  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import User  # noqa: E402

ROOT_DIR = Path(__file__).resolve().parent.parent
TEST_SUB = "auth0|test-user"


def pytest_asyncio_loop_factories(config, item):
    # psycopg's async mode (LangGraph's checkpointer and store) doesn't support Windows' default Proactor loop.
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


def make_schema_engine(schema: str):
    """Engine whose connections default to `schema`, so create_all/queries stay out of `public`."""
    return create_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={schema}"})


@pytest.fixture(scope="session")
def admin_engine():
    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect():
            pass
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable at DATABASE_URL (start it with `docker compose up -d`): {exc}")
    yield engine
    engine.dispose()


@pytest.fixture
def fresh_schema(admin_engine):
    """An empty, uniquely named Postgres schema, dropped after the test."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with admin_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    yield schema
    with admin_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.fixture(scope="session")
def db_engine(admin_engine):
    """Session-wide test schema with all tables created."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with admin_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = make_schema_engine(schema)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    with admin_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.fixture
def db(db_engine):
    """A DB session wrapped in a transaction that is rolled back after the test.

    Also overrides the app's `get_db` so requests see the same data.
    """
    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    fastapi_app.dependency_overrides[get_db] = lambda: session
    yield session
    fastapi_app.dependency_overrides.pop(get_db, None)
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(scope="session")
def redis_server():
    probe = redis.Redis.from_url(TEST_REDIS_URL)
    try:
        probe.ping()
    except redis.RedisError as exc:
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL} (start it with `docker compose up -d`): {exc}")
    finally:
        probe.close()


@pytest.fixture
def redis_db(redis_server, monkeypatch):
    client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    client.flushdb()
    # Every module that does `from app.redis_client import redis_client` needs patching.
    for module in (app.redis_client, app.main, app.auth.session_store, app.auth.redis_state, app.workers.datasets, app.routers.runs, app.routers.usage, app.routers.conversations):
        monkeypatch.setattr(module, "redis_client", client)
    yield client
    client.flushdb()
    client.close()


@pytest.fixture
async def async_redis_db(redis_server, monkeypatch):
    client = redis.asyncio.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    monkeypatch.setattr(app.redis_client, "async_redis_client", client)
    yield client
    await client.flushdb()
    await client.aclose()


def make_client(asgi_app) -> httpx.AsyncClient:
    # https base URL: the session cookie is Secure (https_only=True).
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app), base_url="https://testserver")


@pytest.fixture
async def client(redis_db):
    async with make_client(fastapi_app) as c:
        yield c
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
def claims() -> dict:
    return {"sub": TEST_SUB, "aud": os.environ["AUTH0_AUDIENCE"]}


@pytest.fixture
async def authed_client(client, claims):
    """Client whose requests are authenticated as `claims` (bypasses sessions and JWT verification)."""
    fastapi_app.dependency_overrides[get_current_user] = lambda: claims
    return client


@pytest.fixture
def user(db) -> User:
    db_user = User(auth0_sub=TEST_SUB, email="test@example.com", name="Test User")
    db.add(db_user)
    db.flush()
    return db_user


def session_cookie(session_id: str) -> dict[str, str]:
    """A `Cookie` header carrying a validly signed Starlette `session` cookie for `session_id`."""
    data = b64encode(json.dumps({"session_id": session_id}).encode("utf-8"))
    signed = itsdangerous.TimestampSigner(SESSION_SECRET_KEY).sign(data).decode("utf-8")
    return {"Cookie": f"session={signed}"}


@pytest.fixture
def make_user(db):
    def _make(sub: str) -> User:
        db_user = User(auth0_sub=sub, email=f"{sub.split('|')[-1]}@example.com")
        db.add(db_user)
        db.flush()
        return db_user

    return _make


@pytest.fixture
def login_as():
    """Makes subsequent requests authenticate as `user` (switchable mid-test)."""

    def _login(user: User) -> None:
        fastapi_app.dependency_overrides[get_current_user] = lambda: {"sub": user.auth0_sub}

    return _login
