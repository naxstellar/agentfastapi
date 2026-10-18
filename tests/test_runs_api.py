"""WP4 acceptance for the API side: run creation guards, ownership, cancel flag and the SSE stream."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.models_agent import Conversation, Message, Run
from app.services import events, run_guard
from app.workers.runner import cancel_key
from app.workers.tasks import run_agent

CSRF = {"X-Requested-With": "fetch"}


@pytest.fixture
def enqueued(monkeypatch):
    calls = []
    monkeypatch.setattr(run_agent, "apply_async", lambda **kwargs: calls.append(kwargs))
    return calls


@pytest.fixture(autouse=True)
def events_client(monkeypatch, async_redis_db):
    monkeypatch.setattr(events, "api_events_client", lambda: async_redis_db)
    return async_redis_db


@pytest.fixture
def conversation(db, user):
    conv = Conversation(user_id=user.id, model="anthropic:claude-sonnet-5-5")
    db.add(conv)
    db.flush()
    return conv


async def post_run(client, conversation_id, text="hello"):
    return await client.post(f"/api/v1/conversations/{conversation_id}/runs", json={"text": text}, headers=CSRF)


async def test_create_run_queues_it(authed_client, db, user, conversation, enqueued):
    response = await post_run(authed_client, conversation.id, "how many rows?")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued" and body["conversation_id"] == str(conversation.id)

    run = db.get(Run, uuid.UUID(body["id"]))
    assert run.user_id == user.id
    message = db.scalar(select(Message).where(Message.conversation_id == conversation.id))
    assert message.role == "user" and message.content == {"run_id": body["id"], "text": "how many rows?"}
    assert enqueued == [{"args": [body["id"]], "task_id": body["id"]}]


async def test_second_run_on_a_conversation_is_409(authed_client, conversation, enqueued):
    assert (await post_run(authed_client, conversation.id)).status_code == 202
    assert (await post_run(authed_client, conversation.id)).status_code == 409
    assert len(enqueued) == 1


async def test_409_survives_a_redis_flush(authed_client, db, conversation, enqueued, redis_db):
    assert (await post_run(authed_client, conversation.id)).status_code == 202
    redis_db.flushdb()
    assert (await post_run(authed_client, conversation.id)).status_code == 409
    assert db.scalar(select(Message.id).where(Message.conversation_id == conversation.id).limit(1))
    assert len(db.scalars(select(Message).where(Message.conversation_id == conversation.id)).all()) == 1


async def test_user_cap_is_429(authed_client, db, user, enqueued, monkeypatch):
    monkeypatch.setattr(run_guard, "MAX_CONCURRENT_RUNS", 2)
    conversations = [Conversation(user_id=user.id, model="m") for _ in range(3)]
    db.add_all(conversations)
    db.flush()
    statuses = [(await post_run(authed_client, c.id)).status_code for c in conversations]
    assert statuses == [202, 202, 429]
    refused = await post_run(authed_client, conversations[2].id)
    assert refused.headers["retry-after"]
    # The refused attempt didn't leave its conversation locked or a run behind.
    assert db.scalar(select(Run).where(Run.conversation_id == conversations[2].id)) is None


async def test_enqueue_failure_frees_the_guards(authed_client, db, conversation, monkeypatch, redis_db):
    def broken(**kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(run_agent, "apply_async", broken)
    with pytest.raises(RuntimeError):
        await post_run(authed_client, conversation.id)
    run = db.scalar(select(Run).where(Run.conversation_id == conversation.id))
    assert run.status == "failed"
    assert run_guard.acquire(redis_db, run.user_id, conversation.id, uuid.uuid4()) is run_guard.Admission.OK


async def test_message_is_size_limited(authed_client, conversation, enqueued):
    assert (await post_run(authed_client, conversation.id, "")).status_code == 422
    assert (await post_run(authed_client, conversation.id, "x" * 20_001)).status_code == 422


async def test_foreign_resources_are_404(authed_client, db, user, make_user, login_as, conversation, enqueued):
    other = make_user("auth0|other")
    foreign = Conversation(user_id=other.id, model="m")
    db.add(foreign)
    db.flush()
    foreign_run = Run(conversation_id=foreign.id, user_id=other.id, status="running")
    db.add(foreign_run)
    db.flush()
    login_as(user)

    assert (await post_run(authed_client, foreign.id)).status_code == 404
    assert (await authed_client.get(f"/api/v1/runs/{foreign_run.id}")).status_code == 404
    assert (await authed_client.post(f"/api/v1/runs/{foreign_run.id}/cancel", headers=CSRF)).status_code == 404
    assert (await authed_client.get(f"/api/v1/runs/{foreign_run.id}/events")).status_code == 404


async def test_get_run(authed_client, db, user, conversation):
    run = Run(conversation_id=conversation.id, user_id=user.id, status="running")
    db.add(run)
    db.flush()
    body = (await authed_client.get(f"/api/v1/runs/{run.id}")).json()
    assert body["status"] == "running" and body["id"] == str(run.id)


async def test_cancel_sets_the_flag_only_for_active_runs(authed_client, db, user, conversation, async_redis_db):
    active = Run(conversation_id=conversation.id, user_id=user.id, status="running")
    finished = Run(conversation_id=conversation.id, user_id=user.id, status="succeeded")
    db.add_all([active, finished])
    db.flush()

    assert (await authed_client.post(f"/api/v1/runs/{active.id}/cancel", headers=CSRF)).status_code == 202
    assert await async_redis_db.exists(cancel_key(active.id))
    assert (await authed_client.post(f"/api/v1/runs/{finished.id}/cancel", headers=CSRF)).status_code == 202
    assert not await async_redis_db.exists(cancel_key(finished.id))


async def test_cancel_needs_the_csrf_header(authed_client, db, user, conversation):
    run = Run(conversation_id=conversation.id, user_id=user.id, status="running")
    db.add(run)
    db.flush()
    assert (await authed_client.post(f"/api/v1/runs/{run.id}/cancel")).status_code == 403


# --- SSE ------------------------------------------------------------------------------------------------


def parse_sse(text: str) -> list[dict]:
    parsed = []
    for block in text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            fields[key] = value
        if fields:
            parsed.append(fields)
    return parsed


@pytest.fixture
async def streamed_run(db, user, conversation, async_redis_db):
    run = Run(conversation_id=conversation.id, user_id=user.id, status="succeeded")
    db.add(run)
    db.flush()
    ids = []
    for kind, data in [("run_started", {"run_id": str(run.id)}), ("token", {"text": "Hi"}), ("done", {"status": "succeeded"})]:
        ids.append(await events.publish(async_redis_db, run.id, kind, data))
    return run, ids


async def test_events_replay_from_the_start(authed_client, streamed_run):
    run, ids = streamed_run
    response = await authed_client.get(f"/api/v1/runs/{run.id}/events?from=0")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    sse = parse_sse(response.text)
    assert [e["event"] for e in sse] == ["run_started", "token", "done"]
    assert [e["id"] for e in sse] == ids
    assert json.loads(sse[1]["data"]) == {"text": "Hi"}


async def test_events_resume_after_last_event_id_without_gaps_or_duplicates(authed_client, streamed_run):
    run, ids = streamed_run
    response = await authed_client.get(f"/api/v1/runs/{run.id}/events", headers={"Last-Event-ID": ids[0]})
    sse = parse_sse(response.text)
    assert [e["id"] for e in sse] == ids[1:]


async def test_invalid_last_event_id_is_400(authed_client, streamed_run):
    run, _ = streamed_run
    response = await authed_client.get(f"/api/v1/runs/{run.id}/events", headers={"Last-Event-ID": "abc"})
    assert response.status_code == 400


async def test_expired_stream_is_410(authed_client, db, user, conversation):
    run = Run(conversation_id=conversation.id, user_id=user.id, status="succeeded")
    db.add(run)
    db.flush()
    assert (await authed_client.get(f"/api/v1/runs/{run.id}/events")).status_code == 410


async def test_stale_active_run_does_not_block_the_conversation(authed_client, db, user, conversation, enqueued):
    from datetime import datetime, timedelta, timezone

    old = Run(
        conversation_id=conversation.id, user_id=user.id, status="running",
        created_at=datetime.now(timezone.utc) - timedelta(seconds=run_guard.GUARD_TTL_S + 60),
    )
    db.add(old)
    db.flush()
    assert (await post_run(authed_client, conversation.id)).status_code == 202


async def test_stream_closes_when_the_run_ended_without_a_done_event(authed_client, db, user, conversation, async_redis_db, monkeypatch):
    monkeypatch.setattr(events, "BLOCK_MS", 50)
    run = Run(conversation_id=conversation.id, user_id=user.id, status="failed")
    db.add(run)
    db.flush()
    await events.publish(async_redis_db, run.id, "run_started", {})  # the worker died before `done`
    response = await authed_client.get(f"/api/v1/runs/{run.id}/events?from=0")
    sse = parse_sse(response.text)
    assert [e["event"] for e in sse] == ["run_started", "done"]
    assert json.loads(sse[1]["data"]) == {"status": "failed"}
