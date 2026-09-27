import json
import secrets

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
