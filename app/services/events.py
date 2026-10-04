"""Run event streams (plan §2.4): one Redis stream per run, `run:{run_id}:events`.

The worker `publish()`es; the API's SSE route `tail()`s. The stream id is the SSE `id:`, so a reconnect with
`Last-Event-ID` resumes with no gaps or duplicates. Streams are length-capped and expire after the run ends.
"""

import json
import uuid
from functools import lru_cache
from collections.abc import AsyncIterator

import redis.asyncio

from app.config import APPROVAL_TIMEOUT_S, EVENTS_REDIS_URL, RUN_TIME_LIMIT_S

STREAM_MAXLEN = 5000
# Kept after a run ends so a client that was disconnected can still replay the tail (and the history
# endpoint takes over after that).
STREAM_TTL_S = 3600
# Every publish renews this, so the stream of a run whose worker died before `finish()` still goes away. It outlasts
# the longest run plus an approval pause (those streams must not expire while paused).
SAFETY_TTL_S = RUN_TIME_LIMIT_S + APPROVAL_TIMEOUT_S + STREAM_TTL_S
BLOCK_MS = 15_000  # also the keep-alive interval
TERMINAL_EVENTS = frozenset({"done"})


def stream_key(run_id: uuid.UUID) -> str:
    return f"run:{run_id}:events"


def make_events_client() -> redis.asyncio.Redis:
    """Async clients are bound to the event loop that first uses them: the API and each worker make their own."""
    return redis.asyncio.Redis.from_url(EVENTS_REDIS_URL, decode_responses=True)


@lru_cache(maxsize=1)
def api_events_client() -> redis.asyncio.Redis:
    """The API process's shared client (one event loop). Also carries the cancel flags, which the worker reads."""
    return make_events_client()


async def publish(client: redis.asyncio.Redis, run_id: uuid.UUID, event_type: str, data: dict | None = None) -> str:
    """Appends one event and returns its stream id."""
    key = stream_key(run_id)
    async with client.pipeline(transaction=False) as pipe:
        pipe.xadd(
            key,
            {"type": event_type, "data": json.dumps(data or {}, default=str)},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        pipe.expire(key, SAFETY_TTL_S)
        return (await pipe.execute())[0]


async def finish(client: redis.asyncio.Redis, run_id: uuid.UUID) -> None:
    """Starts the expiry clock. Not called while a run is awaiting approval: it continues on this stream."""
    await client.expire(stream_key(run_id), STREAM_TTL_S)


async def tail(
    client: redis.asyncio.Redis, run_id: uuid.UUID, last_id: str = "0"
) -> AsyncIterator[tuple[str, str, dict] | None]:
    """Yields (stream_id, type, data) for events after `last_id`, ending after a terminal event.

    Yields None when BLOCK_MS passes with nothing new (the caller sends a keep-alive).
    """
    key = stream_key(run_id)
    while True:
        response = await client.xread({key: last_id}, block=BLOCK_MS, count=100)
        if not response:
            yield None
            continue
        for _, entries in response:
            for entry_id, fields in entries:
                last_id = entry_id
                event_type = fields["type"]
                yield entry_id, event_type, json.loads(fields["data"])
                if event_type in TERMINAL_EVENTS:
                    return
