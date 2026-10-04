"""WP5: quota counters, run-end rollup, the usage callback, the run gate and GET /usage."""

import asyncio
import uuid
from contextlib import nullcontext
from datetime import date

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, LLMResult
from sqlalchemy import select

from app.agent import graph as graph_module
from app.agent.graph import build_agent
from app.agent.usage import UsageCallback, tokens_from_result
from app.models_agent import Conversation, Run, TokenUsageDaily
from app.services import events, quota
from app.services.quota import Quota, rollup_run
from app.workers.runner import cancel_key, execute_run
from app.workers.tasks import run_agent
from tests.fakes import ScriptedChatModel
from tests.test_agent_graph import chat_history, is_summary_request
from tests.test_run_worker import wait_for_event, code_then_answer, make_deps, new_run, persistence, reload, stream_events, types  # noqa: F401

CSRF = {"X-Requested-With": "fetch"}


@pytest.fixture
def q(db, fake_redis) -> Quota:
    return Quota(fake_redis, lambda: nullcontext(db))


def daily(db, user) -> tuple[int, int] | None:
    db.expire_all()
    row = db.get(TokenUsageDaily, (user.id, quota.today()))
    return (row.input_tokens, row.output_tokens) if row else None


def new_run_row(db, user) -> Run:
    conversation = Conversation(user_id=user.id, model="m")
    db.add(conversation)
    db.flush()
    run = Run(conversation_id=conversation.id, user_id=user.id)
    db.add(run)
    db.flush()
    return run


# --- counters ---------------------------------------------------------------------------------------------


def test_add_usage_counts_per_run_and_per_day(q, user):
    run_id = uuid.uuid4()
    assert q.add_usage(user.id, run_id, 10, 5) == (10, 5, 15)
    assert q.add_usage(user.id, run_id, 20, 1) == (30, 6, 36)
    assert q.run_usage(run_id) == (30, 6)
    assert q.status(user.id).used == 36


def test_cold_counter_is_warmed_from_postgres(q, db, user, fake_redis):
    db.add(TokenUsageDaily(user_id=user.id, day=quota.today(), input_tokens=700, output_tokens=300))
    db.flush()
    assert q.status(user.id).used == 1000
    fake_redis.flushall()  # Redis flush: the record in Postgres is the fallback
    assert q.add_usage(user.id, uuid.uuid4(), 1, 1)[2] == 1002


def test_status_when_over_budget(q, user, monkeypatch):
    monkeypatch.setattr(quota, "DAILY_TOKEN_BUDGET", 10)
    q.add_usage(user.id, uuid.uuid4(), 6, 4)
    status = q.status(user.id)
    assert status.exceeded and status.remaining == 0 and 1 <= status.retry_after_s <= 86400


def test_rollup_is_idempotent_and_adds_only_the_difference(db, user):
    run = new_run_row(db, user)
    rollup_run(db, run.id, user.id, 100, 40)
    rollup_run(db, run.id, user.id, 100, 40)
    assert daily(db, user) == (100, 40)
    rollup_run(db, run.id, user.id, 130, 40)  # more arrived later (e.g. a redelivery)
    db.expire_all()
    assert daily(db, user) == (130, 40)
    assert (db.get(Run, run.id).input_tokens, db.get(Run, run.id).output_tokens) == (130, 40)


# --- callback token extraction ----------------------------------------------------------------------------


def _result(message=None, llm_output=None) -> LLMResult:
    generations = [[ChatGeneration(message=message or AIMessage("x"))]]
    return LLMResult(generations=generations, llm_output=llm_output)


def test_tokens_from_usage_metadata():
    message = AIMessage("x", usage_metadata={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})
    assert tokens_from_result(_result(message)) == (12, 3)


def test_tokens_fall_back_to_openai_token_usage():
    result = _result(llm_output={"token_usage": {"prompt_tokens": 7, "completion_tokens": 2}})
    assert tokens_from_result(result) == (7, 2)
    assert tokens_from_result(_result()) == (0, 0)


# --- the run ----------------------------------------------------------------------------------------------


def with_usage(responder, per_call=(100, 20)):
    def respond(messages):
        reply = responder(messages)
        reply.usage_metadata = {
            "input_tokens": per_call[0], "output_tokens": per_call[1], "total_tokens": sum(per_call)
        }
        return reply

    return respond


async def test_run_meters_tokens_and_rolls_them_up(db, user, make_deps, new_run, fake_async_redis, monkeypatch):
    monkeypatch.setattr(quota, "DAILY_TOKEN_BUDGET", 1)  # exhausted from the first call; the run still finishes
    run = new_run()
    await execute_run(make_deps(with_usage(code_then_answer)), run.id)  # two model calls

    evts = await stream_events(fake_async_redis, run.id)
    usage = [d for t, d in evts if t == "usage"]
    assert usage == [
        {"input_tokens": 100, "output_tokens": 20, "day_used": 120, "day_limit": 1},
        {"input_tokens": 200, "output_tokens": 40, "day_used": 240, "day_limit": 1},
    ]
    assert types(evts)[-1] == "done"
    run = reload(db, run)
    assert run.status == "succeeded" and (run.input_tokens, run.output_tokens) == (200, 40)
    assert daily(db, user) == (200, 40)


async def test_failed_run_still_records_its_tokens(db, user, make_deps, new_run):
    def responder(messages):
        raise RuntimeError("boom")

    run = new_run()
    deps = make_deps(responder)
    deps.quota.add_usage(user.id, run.id, 50, 5)  # tokens counted before the failure
    await execute_run(deps, run.id)
    run = reload(db, run)
    # The 50/5 counted earlier, plus the estimate for the call that blew up (the prompt, no output).
    assert run.status == "failed" and run.input_tokens > 50 and run.output_tokens == 5
    assert daily(db, user) == (run.input_tokens, 5)


async def test_summarizer_calls_are_counted(persistence, q, user, fake_async_redis, monkeypatch):
    monkeypatch.setattr(graph_module, "SUMMARIZE_TRIGGER_TOKENS", 6000)
    monkeypatch.setattr(graph_module, "SUMMARIZE_KEEP_MESSAGES", 6)

    def respond(messages):
        if is_summary_request(messages):
            reply = AIMessage("SUMMARY of earlier turns")
            reply.usage_metadata = {"input_tokens": 1000, "output_tokens": 10, "total_tokens": 1010}
        else:
            reply = AIMessage("final")
            reply.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        return reply

    run_id = uuid.uuid4()
    callback = UsageCallback(q, fake_async_redis, user.id, run_id, 10**9)
    agent = build_agent(ScriptedChatModel(responder=respond), checkpointer=persistence.checkpointer, store=persistence.store)
    history = chat_history(16, 800)  # past the trigger, so the middleware summarizes before the model call
    await agent.ainvoke(
        {"messages": [*history, HumanMessage("latest question")]},
        config={"configurable": {"thread_id": str(uuid.uuid4())}, "callbacks": [callback]},
    )
    assert q.run_usage(run_id) == (1001, 11)  # the summary call (1000/10) and the answer (1/1)


class HangingStreamModel(ScriptedChatModel):
    """Streams 80 characters, then never finishes (the real runs stream, which is the path that reports errors)."""

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        yield ChatGenerationChunk(message=AIMessageChunk(content="x" * 80))
        await asyncio.sleep(60)


async def test_cancelled_call_is_charged_an_estimate(db, user, make_deps, new_run, fake_async_redis):
    run = new_run("x" * 400)  # ~100 prompt tokens
    model = HangingStreamModel(responder=lambda m: AIMessage(""))
    deps = make_deps(code_then_answer, model_factory=lambda _model_id: model)
    task = asyncio.create_task(execute_run(deps, run.id))
    await wait_for_event(fake_async_redis, run.id, "token")  # the call is streaming
    await fake_async_redis.set(cancel_key(run.id), "1")
    async with asyncio.timeout(2):
        await task
    run = reload(db, run)
    # The prompt (user text plus the system prompt) and the 80 streamed characters are billed although the call never ended.
    assert run.status == "cancelled" and run.input_tokens >= 100 and run.output_tokens == 20
    assert daily(db, user) == (run.input_tokens, 20)


async def test_errored_call_counts_partial_output(q, user, fake_async_redis):
    run_id, call_id = uuid.uuid4(), uuid.uuid4()
    callback = UsageCallback(q, fake_async_redis, user.id, run_id, 10**9)
    await callback.on_chat_model_start({}, [[HumanMessage("y" * 40)]], run_id=call_id)
    await callback.on_llm_new_token("z" * 20, run_id=call_id)
    await callback.on_llm_error(RuntimeError("stream died"), run_id=call_id)
    input_tokens, output_tokens = q.run_usage(run_id)
    assert input_tokens >= 10 and output_tokens == 5
    await callback.on_llm_error(RuntimeError("again"), run_id=call_id)  # nothing left to charge
    assert q.run_usage(run_id) == (input_tokens, output_tokens)


# --- API --------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def api_events(monkeypatch, fake_async_redis):
    monkeypatch.setattr(events, "api_events_client", lambda: fake_async_redis)


@pytest.fixture
def conversation(db, user):
    conv = Conversation(user_id=user.id, model="anthropic:claude-sonnet-5-5")
    db.add(conv)
    db.flush()
    return conv


async def test_exhausted_budget_blocks_new_runs_with_retry_after(authed_client, db, user, conversation, q, monkeypatch):
    enqueued = []
    monkeypatch.setattr(run_agent, "apply_async", lambda **kwargs: enqueued.append(kwargs))
    monkeypatch.setattr(quota, "DAILY_TOKEN_BUDGET", 100)
    url = f"/api/v1/conversations/{conversation.id}/runs"

    assert (await authed_client.post(url, json={"text": "hi"}, headers=CSRF)).status_code == 202
    q.add_usage(user.id, uuid.uuid4(), 80, 30)  # the in-flight run pushes the user over
    refused = await authed_client.post(url, json={"text": "again"}, headers=CSRF)
    assert refused.status_code == 429 and int(refused.headers["retry-after"]) >= 1
    assert len(enqueued) == 1
    # The refusal left nothing behind: no extra run, and the guards weren't taken.
    assert len(db.scalars(select(Run).where(Run.conversation_id == conversation.id)).all()) == 1


async def test_usage_endpoint(authed_client, user, q, monkeypatch):
    monkeypatch.setattr(quota, "DAILY_TOKEN_BUDGET", 1000)
    q.add_usage(user.id, uuid.uuid4(), 300, 100)
    body = (await authed_client.get("/api/v1/usage")).json()
    assert (body["used"], body["limit"], body["remaining"]) == (400, 1000, 600)
    assert date.fromisoformat(body["reset_at"][:10]) > quota.today()
