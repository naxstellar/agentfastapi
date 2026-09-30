import asyncio
import threading
import time

import httpx
import jwt
import pytest

from app.auth import security
from app.auth.session_store import acquire_refresh_lock, create_session, get_session, release_refresh_lock
from tests.conftest import session_cookie

OLD_TOKEN = "expired-access-token"
NEW_TOKEN = "fresh-access-token"


def _fake_decode(token: str) -> dict:
    if token == OLD_TOKEN:
        raise jwt.ExpiredSignatureError("expired")
    return {"sub": "auth0|test-user", "token": token}


@pytest.fixture
def expired_session(fake_redis, monkeypatch) -> str:
    monkeypatch.setattr(security, "_decode_access_token", _fake_decode)
    return create_session({"access_token": OLD_TOKEN, "refresh_token": "rt-1", "claims": {}})


@pytest.fixture
def refresh_calls(monkeypatch) -> list[str]:
    calls: list[str] = []
    lock = threading.Lock()

    def fake_refresh(refresh_token: str) -> dict:
        with lock:
            calls.append(refresh_token)
        time.sleep(0.3)  # keep the refresh in flight while the other requests arrive
        return {"access_token": NEW_TOKEN, "refresh_token": "rt-2"}

    monkeypatch.setattr(security, "refresh_tokens", fake_refresh)
    return calls


async def test_parallel_requests_with_expired_token_refresh_once(client, expired_session, refresh_calls):
    headers = session_cookie(expired_session)

    responses = await asyncio.gather(*(client.get("/api/v1/me", headers=headers) for _ in range(10)))

    assert [r.status_code for r in responses] == [200] * 10
    assert all(r.json()["token"] == NEW_TOKEN for r in responses)
    assert refresh_calls == ["rt-1"]
    session = get_session(expired_session)
    assert session["access_token"] == NEW_TOKEN
    assert session["refresh_token"] == "rt-2"


async def test_sequential_requests_after_refresh_do_not_refresh_again(client, expired_session, refresh_calls):
    headers = session_cookie(expired_session)

    first = await client.get("/api/v1/me", headers=headers)
    second = await client.get("/api/v1/me", headers=headers)

    assert first.status_code == second.status_code == 200
    assert refresh_calls == ["rt-1"]


async def test_session_refreshed_between_read_and_lock_is_not_refreshed_again(
    client, expired_session, refresh_calls, monkeypatch
):
    # Another request finished refreshing after this one read the (expired) session but before it took the lock.
    real_get_session = security.get_session
    reads = 0

    def get_session_then_refreshed(session_id):
        nonlocal reads
        reads += 1
        session = real_get_session(session_id)
        if reads > 1:
            session["access_token"] = NEW_TOKEN
        return session

    monkeypatch.setattr(security, "get_session", get_session_then_refreshed)

    response = await client.get("/api/v1/me", headers=session_cookie(expired_session))

    assert response.status_code == 200
    assert response.json()["token"] == NEW_TOKEN
    assert refresh_calls == []


async def test_waiter_gets_401_not_hang_when_lock_holder_gives_up(client, expired_session, refresh_calls):
    lock = acquire_refresh_lock(expired_session)
    assert lock is not None

    async def release_without_refreshing():
        await asyncio.sleep(0.3)
        release_refresh_lock(expired_session, lock)

    started = time.monotonic()
    response, _ = await asyncio.gather(
        client.get("/api/v1/me", headers=session_cookie(expired_session)), release_without_refreshing()
    )

    assert response.status_code == 401
    assert time.monotonic() - started < 3
    assert refresh_calls == []


async def test_rejected_refresh_token_is_401_and_releases_lock(client, expired_session, monkeypatch):
    def rejected(refresh_token: str) -> dict:
        request = httpx.Request("POST", "https://auth0/oauth/token")
        raise httpx.HTTPStatusError("403", request=request, response=httpx.Response(403, request=request))

    monkeypatch.setattr(security, "refresh_tokens", rejected)

    response = await client.get("/api/v1/me", headers=session_cookie(expired_session))

    assert response.status_code == 401
    assert get_session(expired_session)["access_token"] == OLD_TOKEN
    assert not security.refresh_lock_held(expired_session)


def test_release_does_not_delete_a_lock_owned_by_someone_else(fake_redis):
    mine = acquire_refresh_lock("sid")
    fake_redis.delete("session:sid:refresh_lock")  # simulate expiry
    theirs = acquire_refresh_lock("sid")

    release_refresh_lock("sid", mine)

    assert theirs is not None
    assert security.refresh_lock_held("sid")


async def test_no_session_cookie_is_401(client):
    response = await client.get("/api/v1/me")
    assert response.status_code == 401


async def test_unreachable_auth0_is_503_and_releases_lock(client, expired_session, monkeypatch):
    def unreachable(refresh_token: str) -> dict:
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(security, "refresh_tokens", unreachable)

    response = await client.get("/api/v1/me", headers=session_cookie(expired_session))

    assert response.status_code == 503
    assert not security.refresh_lock_held(expired_session)
