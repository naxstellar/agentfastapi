import json
import secrets

import redis

from app.redis_client import redis_client

SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


def create_session(data: dict) -> str:
    session_id = secrets.token_urlsafe(32)
    redis_client.setex(f"session:{session_id}", SESSION_TTL_SECONDS, json.dumps(data))
    return session_id


def get_session(session_id: str | None) -> dict | None:
    if not session_id:
        return None
    raw = redis_client.get(f"session:{session_id}")
    if not raw:
        return None
    return json.loads(raw)


def update_session(session_id: str, data: dict) -> None:
    redis_client.setex(f"session:{session_id}", SESSION_TTL_SECONDS, json.dumps(data))


def delete_session(session_id: str | None) -> None:
    if session_id:
        redis_client.delete(f"session:{session_id}")


REFRESH_LOCK_TTL_SECONDS = 10


def _refresh_lock_key(session_id: str) -> str:
    return f"session:{session_id}:refresh_lock"


def acquire_refresh_lock(session_id: str) -> str | None:
    """Returns an owner token if the lock was acquired, else None."""
    token = secrets.token_urlsafe(16)
    if redis_client.set(_refresh_lock_key(session_id), token, nx=True, ex=REFRESH_LOCK_TTL_SECONDS):
        return token
    return None


def refresh_lock_held(session_id: str) -> bool:
    return bool(redis_client.exists(_refresh_lock_key(session_id)))


def release_refresh_lock(session_id: str, token: str) -> None:
    # Compare-and-delete so we never release a lock that expired and was re-acquired by another request.
    key = _refresh_lock_key(session_id)
    with redis_client.pipeline() as pipe:
        try:
            pipe.watch(key)
            if pipe.get(key) == token:
                pipe.multi()
                pipe.delete(key)
                pipe.execute()
        except redis.WatchError:
            pass
