"""WP6C: code approvals — pause, approve/edit/reject, resume of the same run, quota and concurrency, expiry, settings."""

import app.config
import time
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import select

from app.models_agent import Message, Run, UserSettings
from app.services import approvals, events, quota, run_guard
from app.workers.runner import cancel_key, execute_run
from app.workers.tasks import run_agent
from tests.test_run_worker import (  # noqa: F401
    code_then_answer,
    make_deps,
    new_run,
    persistence,
    reload,
    stream_events,
    tool_call,
    types,
)

CSRF = {"X-Requested-With": "fetch"}


@pytest.fixture(autouse=True)
def api_events(monkeypatch, fake_async_redis):
    monkeypatch.setattr(events, "api_events_client", lambda: fake_async_redis)


@pytest.fixture
def enqueued(monkeypatch):
    calls = []
    monkeypatch.setattr(run_agent, "apply_async", lambda **kwargs: calls.append(kwargs))
    return calls


@pytest.fixture
def approvals_on(db, user):
    db.add(UserSettings(user_id=user.id, require_code_approval=True))
    db.commit()


def adapting(messages):
    """Runs code; once it has a tool result it reports it (a rejection included)."""
    last = messages[-1]
    if isinstance(last, HumanMessage):
        return tool_call("run_python", {"code": "print('original')"})
    if isinstance(last, ToolMessage):
        return AIMessage(f"[{last.status}] {last.text}")
    return AIMessage("?")


async def pause(deps, run):
    await execute_run(deps, run.id)


async def answer(db, fake_redis, deps, run, enqueued, decision):
    """What POST /approve does, then the worker picking the task up."""
    outcome = approvals.resume_run(db, fake_redis, reload(db, run), decision)
    assert outcome is approvals.Resume.RESUMED
    kwargs = enqueued[-1]
    sent = kwargs["args"][1]
    assert kwargs["args"][0] == str(run.id) and kwargs["task_id"] == str(run.id)
    assert {k: v for k, v in sent.items() if k != "interrupt_ids"} == decision and sent["interrupt_ids"]
    await execute_run(deps, run.id, sent)


async def test_run_pauses_for_approval_and_releases_its_slots(db, user, approvals_on, make_deps, new_run, fake_redis, fake_async_redis):
    run = new_run()
    await pause(make_deps(adapting), run)

    run = reload(db, run)
    assert run.status == "awaiting_approval"
    evts = await stream_events(fake_async_redis, run.id)
    assert "done" not in types(evts)
    assert [d for t, d in evts if t == "approval_required"] == [{"tool_call_id": "call-1", "code": "print('original')"}]
    assert "tool_call" in types(evts) and "tool_result" not in types(evts)  # nothing ran yet
    # No concurrency slot and no conversation lock are held while waiting.
    assert run_guard.acquire(fake_redis, user.id, run.conversation_id, uuid.uuid4()) is run_guard.Admission.OK
    assert fake_redis.zscore(approvals.PENDING_KEY, str(run.id)) is not None


async def test_approve_resumes_the_same_run(db, approvals_on, make_deps, new_run, fake_redis, fake_async_redis, enqueued):
    run = new_run()
    deps = make_deps(adapting)
    await pause(deps, run)
    await answer(db, fake_redis, deps, run, enqueued, {"type": "approve"})

    run = reload(db, run)
    assert run.status == "succeeded"
    evts = await stream_events(fake_async_redis, run.id)
    assert [d for t, d in evts if t == "run_resumed"] == [{"run_id": str(run.id), "reason": "approval"}]
    assert types(evts)[-1] == "done"
    assert [d for t, d in evts if t == "tool_result"][0]["output"].endswith("ran: print('original')")
    assert fake_redis.zscore(approvals.PENDING_KEY, str(run.id)) is None
    message = db.scalars(select(Message).where(Message.conversation_id == run.conversation_id, Message.role == "assistant")).one()
    assert message.content["tools"] == [{"name": "run_python", "ok": True}]  # the pre-pause part is kept


async def test_edited_code_is_what_runs(db, approvals_on, make_deps, new_run, fake_redis, fake_async_redis, enqueued):
    run = new_run()
    deps = make_deps(adapting)
    await pause(deps, run)
    await answer(db, fake_redis, deps, run, enqueued, {"type": "edit", "code": "print('edited')", "message": None})

    outputs = [d["output"] for t, d in await stream_events(fake_async_redis, run.id) if t == "tool_result"]
    assert outputs and outputs[0].endswith("ran: print('edited')")
    assert reload(db, run).status == "succeeded"


async def test_reject_makes_the_agent_adapt(db, approvals_on, make_deps, new_run, fake_redis, fake_async_redis, enqueued):
    run = new_run()
    deps = make_deps(adapting)
    await pause(deps, run)
    await answer(db, fake_redis, deps, run, enqueued, {"type": "reject", "message": "too risky", "code": None})

    run = reload(db, run)
    assert run.status == "succeeded"
    evts = await stream_events(fake_async_redis, run.id)
    result = [d for t, d in evts if t == "tool_result"][0]
    assert "too risky" in result["output"] and "error" in result  # the sandbox never saw the code
    message = db.scalars(select(Message).where(Message.conversation_id == run.conversation_id, Message.role == "assistant")).one()
    assert "[error]" in message.content["text"] and message.content["tools"] == [{"name": "run_python", "ok": False}]


async def test_resume_is_not_quota_gated(db, user, approvals_on, make_deps, new_run, fake_redis, enqueued, monkeypatch):
    run = new_run()
    deps = make_deps(adapting)
    await pause(deps, run)
    monkeypatch.setattr(quota, "DAILY_TOKEN_BUDGET", 1)
    deps.quota.add_usage(user.id, uuid.uuid4(), 500, 500)  # budget gone while the run waits
    assert deps.quota.status(user.id).exceeded
    await answer(db, fake_redis, deps, run, enqueued, {"type": "approve"})
    assert reload(db, run).status == "succeeded"


async def test_resume_takes_a_concurrency_slot_and_is_refused_when_the_user_is_at_the_limit(
    db, user, approvals_on, make_deps, new_run, fake_redis, enqueued, monkeypatch
):
    monkeypatch.setattr(run_guard, "MAX_CONCURRENT_RUNS", 1)
    run = new_run()
    await pause(make_deps(adapting), run)
    other = new_run()  # takes the user's only slot while the first one waits
    assert approvals.resume_run(db, fake_redis, reload(db, run), {"type": "approve"}) is approvals.Resume.USER_AT_LIMIT
    assert reload(db, run).status == "awaiting_approval" and not enqueued
    run_guard.release(fake_redis, user.id, other.conversation_id, other.id)
    assert approvals.resume_run(db, fake_redis, reload(db, run), {"type": "approve"}) is approvals.Resume.RESUMED
    assert approvals.resume_run(db, fake_redis, reload(db, run), {"type": "approve"}) is approvals.Resume.NOT_WAITING  # answered once


async def test_unanswered_approval_is_rejected_after_the_timeout(db, user, approvals_on, make_deps, new_run, fake_redis, fake_async_redis, enqueued, session_factory):
    run = new_run()
    deps = make_deps(adapting)
    await pause(deps, run)

    assert approvals.expire_approvals(session_factory, fake_redis) == 0  # not due yet
    fake_redis.zadd(approvals.PENDING_KEY, {str(run.id): time.time() - 1})
    assert approvals.expire_approvals(session_factory, fake_redis) == 1
    decision = enqueued[-1]["args"][1]
    assert decision["type"] == "reject" and decision["message"] == approvals.TIMEOUT_MESSAGE
    await execute_run(deps, run.id, decision)
    assert reload(db, run).status == "succeeded"
    assert approvals.TIMEOUT_MESSAGE in [d for t, d in await stream_events(fake_async_redis, run.id) if t == "tool_result"][0]["output"]


async def test_expiry_registers_paused_runs_missing_after_a_redis_flush(db, approvals_on, make_deps, new_run, fake_redis, session_factory):
    run = new_run()
    await pause(make_deps(adapting), run)
    fake_redis.flushall()
    approvals.expire_approvals(session_factory, fake_redis)
    assert fake_redis.zscore(approvals.PENDING_KEY, str(run.id)) > time.time()


async def test_cancelling_a_paused_run_finishes_it(db, approvals_on, make_deps, new_run, fake_redis, fake_async_redis, enqueued, authed_client):
    run = new_run()
    deps = make_deps(adapting)
    await pause(deps, run)
    response = await authed_client.post(f"/api/v1/runs/{run.id}/cancel", headers=CSRF)
    assert response.status_code == 202 and enqueued[-1]["args"] == [str(run.id), None]
    await execute_run(deps, run.id, None)
    assert reload(db, run).status == "cancelled"
    assert types(await stream_events(fake_async_redis, run.id))[-1] == "done"


async def test_approvals_off_means_no_pause(db, make_deps, new_run):
    run = new_run()
    await execute_run(make_deps(adapting), run.id)
    assert reload(db, run).status == "succeeded"


# --- API ----------------------------------------------------------------------------------------------------


@pytest.fixture
def session_factory(db):
    from contextlib import contextmanager

    @contextmanager
    def factory():
        yield db

    return factory


async def test_approve_endpoint(authed_client, db, approvals_on, make_deps, new_run, enqueued, user, make_user):
    run = new_run()
    url = f"/api/v1/runs/{run.id}/approve"
    assert (await authed_client.post(url, json={"decision": "approve"}, headers=CSRF)).status_code == 409  # not paused
    await pause(make_deps(adapting), run)

    assert (await authed_client.post(url, json={"decision": "edit"}, headers=CSRF)).status_code == 422  # no code
    assert (await authed_client.post(url, json={"decision": "maybe"}, headers=CSRF)).status_code == 422
    ok = await authed_client.post(url, json={"decision": "edit", "edited_code": "print(2)"}, headers=CSRF)
    assert ok.status_code == 202 and ok.json()["status"] == "queued"
    sent = enqueued[-1]["args"][1]
    assert {k: v for k, v in sent.items() if k != "interrupt_ids"} == {"type": "edit", "code": "print(2)", "message": None}
    assert (await authed_client.post(url, json={"decision": "approve"}, headers=CSRF)).status_code == 409  # already answered


async def test_approve_endpoint_hides_other_users_runs(authed_client, db, approvals_on, make_deps, new_run, make_user):
    other = make_user("auth0|someone-else")
    run = new_run()
    db.execute(Run.__table__.update().where(Run.id == run.id).values(user_id=other.id, status="awaiting_approval"))
    db.commit()
    response = await authed_client.post(f"/api/v1/runs/{run.id}/approve", json={"decision": "approve"}, headers=CSRF)
    assert response.status_code == 404


async def test_a_paused_conversation_refuses_new_runs(authed_client, db, approvals_on, make_deps, new_run, enqueued):
    run = new_run()
    await pause(make_deps(adapting), run)
    response = await authed_client.post(f"/api/v1/conversations/{run.conversation_id}/runs", json={"text": "hi"}, headers=CSRF)
    assert response.status_code == 409


async def test_settings_roundtrip(authed_client, user):
    models = list(app.config.ALLOWED_MODELS)
    assert (await authed_client.get("/api/v1/settings")).json() == {
        "require_code_approval": False, "default_model": None, "allowed_models": models}
    body = {"require_code_approval": True, "default_model": None}
    assert (await authed_client.put("/api/v1/settings", json=body, headers=CSRF)).json() == {**body, "allowed_models": models}
    assert (await authed_client.get("/api/v1/settings")).json() == {**body, "allowed_models": models}
    bad = {"require_code_approval": True, "default_model": "openai:not-allowed"}
    assert (await authed_client.put("/api/v1/settings", json=bad, headers=CSRF)).status_code == 422


# --- review fixes -------------------------------------------------------------------------------------------


async def test_a_stale_decision_does_not_answer_a_later_pause(db, approvals_on, make_deps, new_run, fake_async_redis):
    run = new_run()
    deps = make_deps(adapting)
    await pause(deps, run)
    # A redelivered task still carries a decision for an earlier pause (other interrupt ids).
    db.execute(Run.__table__.update().where(Run.id == run.id).values(status="queued"))
    db.commit()
    await execute_run(deps, run.id, {"type": "approve", "interrupt_ids": ["some-earlier-pause"]})

    assert reload(db, run).status == "awaiting_approval"  # paused again, nothing was approved on the user's behalf
    evts = await stream_events(fake_async_redis, run.id)
    assert "tool_result" not in types(evts)
    assert types(evts).count("approval_required") == 2
    assert [d["reason"] for t, d in evts if t == "run_resumed"] == ["redelivery"]


async def test_the_run_is_marked_paused_before_the_approval_event_is_published(db, approvals_on, make_deps, new_run, monkeypatch):
    seen = []
    real = events.publish

    async def spy(client, run_id, event_type, data):
        if event_type == "approval_required":
            db.expire_all()
            seen.append(db.get(Run, run_id).status)
        return await real(client, run_id, event_type, data)

    monkeypatch.setattr(events, "publish", spy)
    run = new_run()
    await pause(make_deps(adapting), run)
    assert seen == ["awaiting_approval"]


async def test_cancelling_a_paused_run_keeps_what_it_did_before_the_pause(db, approvals_on, make_deps, new_run, enqueued, authed_client):
    def responder(messages):
        if isinstance(messages[-1], HumanMessage):
            return AIMessage("Let me check the data.", tool_calls=[{"name": "run_python", "args": {"code": "1"}, "id": "c1", "type": "tool_call"}])
        return AIMessage("?")

    run = new_run()
    deps = make_deps(responder)
    await pause(deps, run)
    await authed_client.post(f"/api/v1/runs/{run.id}/cancel", headers=CSRF)
    await execute_run(deps, run.id, None)
    assert reload(db, run).status == "cancelled"
    message = db.scalars(select(Message).where(Message.conversation_id == run.conversation_id, Message.role == "assistant")).one()
    assert message.content["text"] == "Let me check the data."


async def test_a_failed_cancel_hand_off_leaves_no_cancel_flag(db, approvals_on, make_deps, new_run, fake_async_redis, authed_client, monkeypatch):
    run = new_run()
    await pause(make_deps(adapting), run)

    def broken(**kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(run_agent, "apply_async", broken)
    with pytest.raises(RuntimeError):
        await authed_client.post(f"/api/v1/runs/{run.id}/cancel", headers=CSRF)
    assert reload(db, run).status == "awaiting_approval"
    assert not await fake_async_redis.exists(cancel_key(run.id))
