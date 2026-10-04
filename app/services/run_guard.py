"""Admission control for runs: one active run per conversation (409) and a per-user cap (429).

Both live in Redis so every API replica and worker sees them. Release is idempotent and safe to call from
`finally` blocks: the conversation lock only releases for its own run id, and the per-user set removes by
run id (a counter could drift on double release). The per-user set is sorted by each run's own expiry, so a leaked
entry (crashed worker) ages out by itself instead of being kept alive by the user's later runs; the
conversation lock carries a plain TTL.
"""

import time
import uuid
from enum import Enum

import redis

from app.config import MAX_CONCURRENT_RUNS, RUN_TIME_LIMIT_S

# Longer than the longest run (and its broker redelivery window), so a live run never loses its guard.
GUARD_TTL_S = RUN_TIME_LIMIT_S + 900


class Admission(Enum):
    OK = "ok"
    CONVERSATION_BUSY = "conversation_busy"
    USER_AT_LIMIT = "user_at_limit"


def _lock_key(conversation_id: uuid.UUID) -> str:
    return f"conv:{conversation_id}:active_run"


def _user_key(user_id: uuid.UUID) -> str:
    return f"user:{user_id}:active_runs"


def acquire(client: redis.Redis, user_id: uuid.UUID, conversation_id: uuid.UUID, run_id: uuid.UUID) -> Admission:
    if not client.set(_lock_key(conversation_id), str(run_id), nx=True, ex=GUARD_TTL_S):
        return Admission.CONVERSATION_BUSY
    user_key = _user_key(user_id)
    now = time.time()
    with client.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(user_key, "-inf", now)  # leaked entries of crashed runs
        pipe.zadd(user_key, {str(run_id): now + GUARD_TTL_S})
        pipe.expire(user_key, GUARD_TTL_S)
        pipe.zcard(user_key)
        count = pipe.execute()[-1]
    if count > MAX_CONCURRENT_RUNS:
        release(client, user_id, conversation_id, run_id)
        return Admission.USER_AT_LIMIT
    return Admission.OK


def release(client: redis.Redis, user_id: uuid.UUID, conversation_id: uuid.UUID, run_id: uuid.UUID) -> None:
    key = _lock_key(conversation_id)
    # Check-then-delete is safe: while this run holds the lock nobody else can take it (only a TTL expiry
    # could, and that is longer than any run).
    if client.get(key) == str(run_id):
        client.delete(key)
    client.zrem(_user_key(user_id), str(run_id))
