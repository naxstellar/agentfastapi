"""WP4 acceptance for the worker side: execute_run() with a scripted chat model, the fake sandbox, fakeredis
event streams and the real Postgres checkpointer (in a throwaway schema)."""

import asyncio
import threading
import uuid
from contextlib import contextmanager

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import select

from app.agent.graph import open_persistence, psycopg_conninfo
from app.agent.history import add_user_message
from app.agent.models import UnknownModelError
from app.agent.sandbox import SandboxManager, SandboxSlots
from app.config import DATABASE_URL
from app.models_agent import Conversation, Message, Run
from app.services import events, run_guard
from app.workers import runner
from app.workers.runner import Outcome, RunDeps, cancel_key, execute_run
from tests.fakes import FakeSandboxProvider, FakeStorage, ScriptedChatModel


async def no_sleep(_):
    pass


@pytest.fixture
async def persistence(fresh_schema):
    async with open_persistence(
        psycopg_conninfo(DATABASE_URL), max_size=2, connect_kwargs={"options": f"-csearch_path={fresh_schema}"}
    ) as p:
        yield p


@pytest.fixture
def make_deps(db, persistence, fake_redis, fake_async_redis):
    """Builds RunDeps around a scripted model; `responder` decides what the "LLM" says."""

    @contextmanager
    def session_factory():
        yield db

    manager = SandboxManager(
        FakeSandboxProvider(), SandboxSlots(fake_async_redis, 5), ttl_s=3600, idle_timeout_s=300, exec_timeout_s=5,
        sleep=no_sleep,
    )

    def _make(responder, **overrides) -> RunDeps:
        model = ScriptedChatModel(responder=responder)
        values = dict(
            persistence=persistence,
            manager=manager,
            storage=FakeStorage(),
            events_client=fake_async_redis,
            guard_client=fake_redis,
            session_factory=session_factory,
            model_factory=lambda _model_id: model,
            cancel_poll_s=0.02,
        )
        values.update(overrides)
        deps = RunDeps(**values)
        deps.model = model
        return deps

    return _make


@pytest.fixture
def new_run(db, user, fake_redis):
    """A queued run with its user message and guards, as the API leaves it."""

    def _new(text="how many rows?") -> Run:
        conversation = Conversation(user_id=user.id, model="anthropic:claude-sonnet-5-5")
        db.add(conversation)
        db.flush()
        run = Run(conversation_id=conversation.id, user_id=user.id, status="queued")
        db.add(run)
        db.flush()
        add_user_message(db, conversation.id, run.id, text)
        db.commit()
        assert run_guard.acquire(fake_redis, user.id, conversation.id, run.id) is run_guard.Admission.OK
        return run

    return _new


async def stream_events(client, run_id):
    entries = await client.xrange(events.stream_key(run_id))
    return [(fields["type"], __import__("json").loads(fields["data"])) for _, fields in entries]


def types(evts):
    return [t for t, _ in evts]


def tool_call(name, args, call_id="call-1"):
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def code_then_answer(messages):
    last = messages[-1]
    if isinstance(last, HumanMessage):
        return tool_call("run_python", {"code": "print(1 + 1)"})
    if isinstance(last, ToolMessage):
        return AIMessage(f"Done. Tool said: {last.text}")
    return AIMessage("?")


def reload(db, run):
    db.expire_all()
    return db.get(Run, run.id)


def assistant_messages(db, run):
    return db.scalars(
        select(Message).where(Message.conversation_id == run.conversation_id, Message.role == "assistant")
    ).all()


# --- happy path -----------------------------------------------------------------------------------------


async def test_run_streams_events_and_finalizes(db, make_deps, new_run, fake_redis, fake_async_redis):
    run = new_run()
    deps = make_deps(code_then_answer)
    await execute_run(deps, run.id)

    evts = await stream_events(fake_async_redis, run.id)
    kinds = types(evts)
    assert kinds[0] == "run_started" and kinds[-1] == "done"
    assert kinds.index("tool_call") < kinds.index("tool_result") < kinds.index("token", kinds.index("tool_result"))
    call = next(d for t, d in evts if t == "tool_call")
    assert call["name"] == "run_python" and call["tool_call_id"] == "call-1"
    result = next(d for t, d in evts if t == "tool_result")
    assert result["tool_call_id"] == "call-1" and "error" not in result
    assert evts[-1][1] == {"status": "succeeded"}
    assert await fake_async_redis.ttl(events.stream_key(run.id)) > 0  # expiry clock started

    run = reload(db, run)
    assert run.status == "succeeded" and run.started_at and run.finished_at and run.error is None
    (reply,) = assistant_messages(db, run)
    assert "Done. Tool said" in reply.content["text"] and reply.content["tools"] == [{"name": "run_python", "ok": True}]
    # The slot and the conversation lock are free again.
    assert run_guard.acquire(fake_redis, run.user_id, run.conversation_id, uuid.uuid4()) is run_guard.Admission.OK


async def test_summarizer_tokens_are_not_relayed(make_deps, new_run, fake_async_redis):
    run = new_run()
    deps = make_deps(lambda messages: AIMessage("hello"))
    await execute_run(deps, run.id)
    evts = await stream_events(fake_async_redis, run.id)
    assert [d["text"] for t, d in evts if t == "token"] == ["hello"]


# --- cancel, timeout, failure ---------------------------------------------------------------------------


def blocking_responder(gate: threading.Event):
    def respond(messages):
        gate.wait(timeout=10)
        return AIMessage("late")

    return respond


async def wait_for_event(client, run_id, kind, timeout=5.0):
    async with asyncio.timeout(timeout):
        while kind not in types(await stream_events(client, run_id)):
            await asyncio.sleep(0.01)


async def test_cancel_mid_stream(db, make_deps, new_run, fake_async_redis):
    run = new_run()
    gate = threading.Event()
    deps = make_deps(blocking_responder(gate))
    task = asyncio.create_task(execute_run(deps, run.id))
    try:
        await wait_for_event(fake_async_redis, run.id, "run_started")
        await fake_async_redis.set(cancel_key(run.id), "1")
        async with asyncio.timeout(2):  # cancel must not wait for the blocked LLM call
            await task
    finally:
        gate.set()

    evts = await stream_events(fake_async_redis, run.id)
    assert evts[-1] == ("done", {"status": "cancelled"})
    assert reload(db, run).status == "cancelled"


async def test_cancel_while_queued_never_calls_the_model(db, make_deps, new_run, fake_async_redis):
    run = new_run()
    await fake_async_redis.set(cancel_key(run.id), "1")
    deps = make_deps(code_then_answer)
    await execute_run(deps, run.id)
    assert deps.model.calls == []
    assert types(await stream_events(fake_async_redis, run.id)) == ["done"]
    assert reload(db, run).status == "cancelled"


async def test_deadline_fails_the_run(db, make_deps, new_run, fake_async_redis):
    run = new_run()
    gate = threading.Event()
    deps = make_deps(blocking_responder(gate), run_time_limit_s=0.2)
    try:
        await execute_run(deps, run.id)
    finally:
        gate.set()
    evts = await stream_events(fake_async_redis, run.id)
    assert evts[-2][0] == "error" and evts[-2][1]["code"] == "timeout"
    assert evts[-1] == ("done", {"status": "failed"})
    assert reload(db, run).status == "failed"


async def test_model_error_fails_with_a_generic_message(db, make_deps, new_run, fake_async_redis):
    def boom(messages):
        raise RuntimeError("secret-api-key-in-message")

    run = new_run()
    await execute_run(make_deps(boom), run.id)
    evts = await stream_events(fake_async_redis, run.id)
    assert evts[-2] == ("error", {"message": "The run failed unexpectedly", "code": "internal"})
    assert "secret" not in repr(evts) and "secret" not in (reload(db, run).error or "")


async def test_unavailable_model_fails_the_run(db, make_deps, new_run, fake_async_redis):
    def no_model(_model_id):
        raise UnknownModelError("nope")

    run = new_run()
    await execute_run(make_deps(code_then_answer, model_factory=no_model), run.id)
    evts = await stream_events(fake_async_redis, run.id)
    assert evts[-2][1]["code"] == "model_unavailable"
    assert reload(db, run).status == "failed"


# --- redelivery -----------------------------------------------------------------------------------------


async def test_redelivered_run_resumes_from_the_checkpoint(db, make_deps, new_run, fake_redis, fake_async_redis, persistence):
    run = new_run()
    gate = threading.Event()

    def respond(messages):
        if isinstance(messages[-1], ToolMessage):  # the second model call: the "worker" dies here
            gate.wait(timeout=10)
        return code_then_answer(messages)

    deps = make_deps(respond)
    first = asyncio.create_task(execute_run(deps, run.id))
    await wait_for_event(fake_async_redis, run.id, "tool_result")
    first.cancel()  # worker shutdown mid-run
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()

    # Not finalized: still running, guards held, no `done`.
    assert reload(db, run).status == "running"
    assert "done" not in types(await stream_events(fake_async_redis, run.id))
    assert run_guard.acquire(fake_redis, run.user_id, run.conversation_id, uuid.uuid4()) is run_guard.Admission.CONVERSATION_BUSY

    await execute_run(deps, run.id)  # the broker redelivers

    evts = await stream_events(fake_async_redis, run.id)
    kinds = types(evts)
    assert kinds.count("run_started") == 1 and "run_resumed" in kinds and kinds[-1] == "done"
    assert next(d for t, d in evts if t == "run_resumed")["reason"] == "redelivery"
    assert kinds.count("tool_call") == 1  # the tool step wasn't repeated

    state = await runner_state(deps, run)
    assert [m.text for m in state if isinstance(m, HumanMessage)] == ["how many rows?"]  # not sent twice
    assert reload(db, run).status == "succeeded"
    (reply,) = assistant_messages(db, run)
    assert reply.content["tools"] == [{"name": "run_python", "ok": True}]  # seeded from the checkpoint


async def runner_state(deps, run):
    from app.agent.graph import build_agent, run_config

    agent = build_agent(deps.model, checkpointer=deps.persistence.checkpointer, store=deps.persistence.store)
    snapshot = await agent.aget_state(run_config(run.conversation_id, run.id))
    return snapshot.values["messages"]


async def test_finished_run_is_not_rerun_and_gets_its_done_event(db, make_deps, new_run, fake_redis, fake_async_redis):
    run = new_run()
    deps = make_deps(code_then_answer)
    await execute_run(deps, run.id)
    await fake_async_redis.delete(events.stream_key(run.id))  # a delivery that died before publishing `done`
    calls = len(deps.model.calls)

    await execute_run(deps, run.id)

    assert len(deps.model.calls) == calls
    assert types(await stream_events(fake_async_redis, run.id)) == ["done"]
    assert len(assistant_messages(db, run)) == 1


async def test_finalizing_twice_saves_one_reply(db, make_deps, new_run):
    run = new_run()
    deps = make_deps(code_then_answer)
    content = {"text": "hi", "tools": [], "artifacts": []}
    assert runner._finalize_db(deps, run, Outcome("succeeded"), content) is True
    assert runner._finalize_db(deps, run, Outcome("succeeded"), content) is False
    assert len(assistant_messages(db, run)) == 1


# --- failure paths: no stuck runs ------------------------------------------------------------------------


async def assert_failed_cleanly(db, run, fake_redis, fake_async_redis, code="internal"):
    evts = await stream_events(fake_async_redis, run.id)
    assert evts[-2][0] == "error" and evts[-2][1]["code"] == code
    assert evts[-1] == ("done", {"status": "failed"})
    assert reload(db, run).status == "failed"
    assert run_guard.acquire(fake_redis, run.user_id, run.conversation_id, uuid.uuid4()) is run_guard.Admission.OK


async def test_setup_failure_fails_the_run_instead_of_stranding_it(db, make_deps, new_run, fake_redis, fake_async_redis, monkeypatch):
    run = new_run()
    real = runner._mark_running
    calls = []

    def flaky(deps, run_id):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("postgres blip")
        return real(deps, run_id)

    monkeypatch.setattr(runner, "_mark_running", flaky)
    await execute_run(make_deps(code_then_answer), run.id)
    await assert_failed_cleanly(db, run, fake_redis, fake_async_redis)


async def test_finalize_failure_fails_the_run(db, make_deps, new_run, fake_redis, fake_async_redis, monkeypatch):
    run = new_run()
    real = runner._finalize_db
    calls = []

    def flaky(deps, run_, outcome, content):
        calls.append(outcome.status)
        if len(calls) == 1:
            raise RuntimeError("postgres blip")
        return real(deps, run_, outcome, content)

    monkeypatch.setattr(runner, "_finalize_db", flaky)
    await execute_run(make_deps(code_then_answer), run.id)
    assert calls == ["succeeded", "failed"]
    await assert_failed_cleanly(db, run, fake_redis, fake_async_redis)


async def test_unrecoverable_failure_still_releases_the_guards(db, make_deps, new_run, fake_redis, monkeypatch):
    run = new_run()

    def down(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(runner, "_mark_running", down)
    monkeypatch.setattr(runner, "_finalize_db", down)
    with pytest.raises(RuntimeError):
        await execute_run(make_deps(code_then_answer), run.id)
    assert run_guard.acquire(fake_redis, run.user_id, run.conversation_id, uuid.uuid4()) is run_guard.Admission.OK


async def test_final_events_are_retried(db, make_deps, new_run, fake_async_redis, monkeypatch):
    run = new_run()
    real = events.publish
    failed = []

    async def flaky(client, run_id, kind, data=None):
        if kind == "done" and not failed:
            failed.append(1)
            raise ConnectionError("redis blip")
        return await real(client, run_id, kind, data)

    monkeypatch.setattr(events, "publish", flaky)
    await execute_run(make_deps(code_then_answer), run.id)
    assert failed and types(await stream_events(fake_async_redis, run.id))[-1] == "done"
    assert reload(db, run).status == "succeeded"


class FlakyExists:
    """Delegates to a fake Redis, but some `exists` calls fail (by call number)."""

    def __init__(self, inner, failing_calls):
        self._inner, self._failing, self.calls = inner, set(failing_calls), 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def exists(self, *keys):
        self.calls += 1
        if self.calls in self._failing:
            raise ConnectionError("redis blip")
        return await self._inner.exists(*keys)


async def test_redis_blip_in_the_cancel_watcher_is_not_a_cancel(db, make_deps, new_run, fake_async_redis):
    run = new_run()
    # Call 1 is the start-up check; the watcher's first polls fail.
    client = FlakyExists(fake_async_redis, failing_calls={2, 3, 4})
    await execute_run(make_deps(code_then_answer, events_client=client), run.id)
    assert client.calls >= 4
    assert reload(db, run).status == "succeeded"


# --- cancelled mid-tool -----------------------------------------------------------------------------------


def test_dangling_tool_calls_are_closed():
    from app.workers.runner import _close_dangling_tool_calls

    open_call = tool_call("run_python", {"code": "x"}, "c1")
    assert [m.tool_call_id for m in _close_dangling_tool_calls([HumanMessage("q"), open_call])] == ["c1"]
    answered = [HumanMessage("q"), open_call, ToolMessage("out", tool_call_id="c1")]
    assert _close_dangling_tool_calls(answered) == []
    assert _close_dangling_tool_calls([HumanMessage("q"), AIMessage("a")]) == []
    assert _close_dangling_tool_calls([]) == []


async def test_next_run_after_a_run_died_mid_tool_is_well_formed(db, make_deps, new_run, persistence):
    from app.agent.graph import build_agent, run_config

    first = new_run("first")
    seen = []

    def respond(messages):
        seen.append(list(messages))
        return AIMessage("ok")

    deps = make_deps(respond)
    agent = build_agent(deps.model, checkpointer=persistence.checkpointer, store=persistence.store)
    # The state a run leaves when it is cancelled during its tool step.
    await agent.aupdate_state(
        run_config(first.conversation_id, first.id),
        {"messages": [HumanMessage("first"), tool_call("run_python", {"code": "slow()"}, "dead-call")]},
    )
    first.status = "cancelled"
    second = Run(conversation_id=first.conversation_id, user_id=first.user_id, status="queued")
    db.add(second)
    db.flush()
    add_user_message(db, first.conversation_id, second.id, "second")
    db.commit()

    await execute_run(deps, second.id)

    sent = seen[-1]
    call_index = next(i for i, m in enumerate(sent) if isinstance(m, AIMessage) and m.tool_calls)
    assert isinstance(sent[call_index + 1], ToolMessage) and sent[call_index + 1].tool_call_id == "dead-call"
    assert isinstance(sent[-1], HumanMessage) and sent[-1].text == "second"
    assert reload(db, second).status == "succeeded"


# --- event stream size ------------------------------------------------------------------------------------


async def test_token_deltas_are_coalesced_in_order(fake_async_redis):
    run_id = uuid.uuid4()
    deps = RunDeps(None, None, None, fake_async_redis, None, None)
    buffer = runner._TokenBuffer(deps, run_id)
    deltas = [f"w{i} " for i in range(400)]
    for delta in deltas:
        await buffer.add(delta)
    await buffer.flush()
    evts = await stream_events(fake_async_redis, run_id)
    assert "".join(d["text"] for _, d in evts) == "".join(deltas)
    assert len(evts) < len(deltas) / 5


async def test_stream_gets_a_safety_expiry_on_every_publish(fake_async_redis):
    run_id = uuid.uuid4()
    await events.publish(fake_async_redis, run_id, "run_started", {})
    assert await fake_async_redis.ttl(events.stream_key(run_id)) > events.STREAM_TTL_S
