"""Executes one agent run (plan §2.8): LangGraph stream -> Redis event stream, with cancel, deadline,
redelivery handling and idempotent finalization.

Everything here is async and runs on the worker's single event loop (see tasks.py). `RunDeps` carries the
per-process resources so tests can inject fakes.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone

import redis
import redis.asyncio
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.agent.context import make_context
from app.agent.graph import Persistence, build_agent, run_config
from app.agent.history import TurnRecorder, save_assistant_message
from app.agent.models import ModelNotConfiguredError, UnknownModelError, make_chat_model
from app.agent.sandbox import SandboxManager
from app.config import RUN_TIME_LIMIT_S
from app.models_agent import Conversation, Message, Run
from app.services import events, run_guard
from app.services.storage import Storage

logger = logging.getLogger("app.workers.runner")

ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")
CANCEL_POLL_S = 0.25
CANCEL_FLAG_TTL_S = 3600
TOOL_OUTPUT_EVENT_CHARS = 20_000
# Events the graph may emit through `emit()` (plan §2.4); anything else is dropped rather than relayed.
CUSTOM_EVENT_TYPES = frozenset({"status", "artifact"})


def cancel_key(run_id: uuid.UUID) -> str:
    return f"run:{run_id}:cancel"


@dataclass
class RunDeps:
    persistence: Persistence
    manager: SandboxManager
    storage: Storage
    events_client: redis.asyncio.Redis  # the run's event streams (and the cancel flag)
    guard_client: redis.Redis  # sync client for run_guard
    session_factory: sessionmaker[Session]
    model_factory: Callable[[str], BaseChatModel] = make_chat_model
    cancel_poll_s: float = CANCEL_POLL_S
    run_time_limit_s: float = RUN_TIME_LIMIT_S


@dataclass
class Outcome:
    status: str  # succeeded | failed | cancelled
    error: str | None = None
    code: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load(deps: RunDeps, run_id: uuid.UUID) -> tuple[Run, str, str] | None:
    """(run, model id, user text) detached from the session, or None when the run is gone."""
    with deps.session_factory() as db:
        run = db.get(Run, run_id)
        if run is None:
            return None
        conversation = db.get(Conversation, run.conversation_id)
        if conversation is None:
            return None
        text = db.scalar(
            select(Message.content["text"].astext).where(
                Message.conversation_id == run.conversation_id,
                Message.role == "user",
                Message.content["run_id"].astext == str(run_id),
            )
        )
        db.expunge(run)
        return run, conversation.model, text or ""


def _mark_running(deps: RunDeps, run_id: uuid.UUID) -> bool:
    """queued/running -> running. False if the run is already terminal."""
    with deps.session_factory() as db:
        result = db.execute(
            update(Run)
            .where(Run.id == run_id, Run.status.in_(ACTIVE_STATUSES))
            .values(status="running", started_at=_utcnow())
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return result.rowcount == 1


def _finalize_db(deps: RunDeps, run: Run, outcome: Outcome, content: dict | None) -> bool:
    """Writes the terminal status and the display message once. False if another delivery already finished it."""
    with deps.session_factory() as db:
        result = db.execute(
            update(Run)
            .where(Run.id == run.id, Run.status.in_(ACTIVE_STATUSES))
            .values(status=outcome.status, error=outcome.error, finished_at=_utcnow())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            return False
        if content and (content.get("text") or content.get("tools")):
            save_assistant_message(db, run.conversation_id, run.id, content)
        db.commit()
        return True


async def _watch_cancel(deps: RunDeps, run_id: uuid.UUID) -> None:
    key = cancel_key(run_id)
    while True:
        try:
            if await deps.events_client.exists(key):
                return
        except Exception as exc:  # a Redis blip must not look like a cancellation: keep polling
            logger.warning("Cancel check for run id=%s failed: %s", run_id, type(exc).__name__)
        await asyncio.sleep(deps.cancel_poll_s)


async def _stream(deps: RunDeps, run: Run, model_id: str, text: str, recorder: TurnRecorder) -> None:
    agent = build_agent(
        deps.model_factory(model_id), checkpointer=deps.persistence.checkpointer, store=deps.persistence.store
    )
    config = run_config(run.conversation_id, run.id)
    context = make_context(
        user_id=run.user_id,
        conversation_id=run.conversation_id,
        run_id=run.id,
        manager=deps.manager,
        storage=deps.storage,
        session_factory=deps.session_factory,
    )

    # Redelivery: the first checkpoint written for this run carries its run_id in the metadata. If it exists,
    # the user message is already applied, so resume with None instead of sending it twice.
    snapshot = await agent.aget_state(config)
    resumed = (snapshot.metadata or {}).get("run_id") == str(run.id)
    if resumed:
        _seed_from_checkpoint(recorder, snapshot.values.get("messages", []), run.id)
        await events.publish(deps.events_client, run.id, "run_resumed", {"run_id": str(run.id), "reason": "redelivery"})
        graph_input = None
        if not snapshot.next:  # crashed after the graph finished, before finalizing
            return
    else:
        await events.publish(deps.events_client, run.id, "run_started", {"run_id": str(run.id)})
        # An earlier run that was cancelled or failed mid-tool leaves tool calls without results, which providers
        # reject; close them first (the reducer appends in order).
        closing = _close_dangling_tool_calls(snapshot.values.get("messages", []))
        graph_input = {"messages": [*closing, HumanMessage(text, id=f"run-{run.id}")]}

    tokens = _TokenBuffer(deps, run.id)
    try:
        async for mode, chunk in agent.astream(
            graph_input, config=config, context=context, stream_mode=["messages", "updates", "custom"]
        ):
            if mode == "messages":
                message, metadata = chunk
                # A streaming model sends AIMessageChunks; a non-streaming one sends the whole AIMessage once.
                if (
                    isinstance(message, AIMessage)
                    and metadata.get("langgraph_node") == "model"
                    and metadata.get("lc_source") != "summarization"
                    and (delta := message.text)
                ):
                    await tokens.add(delta)
                continue
            await tokens.flush()  # keep tokens ordered before the event that follows them
            if mode == "updates":
                recorder.add_update(chunk)
                for node, values in chunk.items():
                    if isinstance(values, dict):
                        await _publish_update(deps, run.id, node, values.get("messages", []))
            elif mode == "custom" and isinstance(chunk, dict) and chunk.get("type") in CUSTOM_EVENT_TYPES:
                await events.publish(deps.events_client, run.id, chunk["type"], chunk.get("data") or {})
    finally:
        with suppress(Exception):
            await tokens.flush()


class _TokenBuffer:
    """Coalesces token deltas into fewer stream entries (a long answer is thousands of tiny deltas, which would
    flood the length-capped stream and push `run_started` out of a replay)."""

    MAX_DELAY_S = 0.05
    MAX_CHARS = 200

    def __init__(self, deps: RunDeps, run_id: uuid.UUID):
        self._deps, self._run_id = deps, run_id
        self._parts: list[str] = []
        self._chars = 0
        self._last_flush = 0.0

    async def add(self, delta: str) -> None:
        self._parts.append(delta)
        self._chars += len(delta)
        if self._chars >= self.MAX_CHARS or time.monotonic() - self._last_flush >= self.MAX_DELAY_S:
            await self.flush()

    async def flush(self) -> None:
        if not self._parts:
            return
        text, self._parts, self._chars = "".join(self._parts), [], 0
        self._last_flush = time.monotonic()
        await events.publish(self._deps.events_client, self._run_id, "token", {"text": text})


def _close_dangling_tool_calls(messages: list) -> list[ToolMessage]:
    """Results for tool calls of the last AI message that never got one (the run died during the tool step)."""
    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    if last_ai is None:
        return []
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return [
        ToolMessage("The run stopped before this tool finished.", tool_call_id=call["id"], name=call["name"], status="error")
        for call in last_ai.tool_calls
        if call["id"] not in answered
    ]


def _seed_from_checkpoint(recorder: TurnRecorder, messages: list, run_id: uuid.UUID) -> None:
    """On redelivery the recorder only sees post-resume updates; rebuild the earlier part of this run's turn."""
    marker = f"run-{run_id}"
    start = next((i for i, m in enumerate(messages) if getattr(m, "id", None) == marker), None)
    if start is None:
        return
    recorder.messages.extend(m for m in messages[start + 1 :] if isinstance(m, (AIMessage, ToolMessage)))


async def _publish_update(deps: RunDeps, run_id: uuid.UUID, node: str, messages: list) -> None:
    if node == "model":
        for message in messages:
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    await events.publish(
                        deps.events_client,
                        run_id,
                        "tool_call",
                        {"tool_call_id": call["id"], "name": call["name"], "args": call["args"]},
                    )
    elif node == "tools":
        for message in messages:
            if isinstance(message, ToolMessage):
                output = message.text
                data = {"tool_call_id": message.tool_call_id, "output": output[:TOOL_OUTPUT_EVENT_CHARS]}
                if message.status == "error":
                    data["error"] = output[:500]
                await events.publish(deps.events_client, run_id, "tool_result", data)


async def _run_to_outcome(deps: RunDeps, run: Run, model_id: str, text: str, recorder: TurnRecorder) -> Outcome:
    """Streams the agent until it finishes, is cancelled, times out or fails. Never raises, except that a
    CancelledError aimed at this coroutine (worker shutdown) propagates so the run is redelivered."""
    stream = asyncio.create_task(_stream(deps, run, model_id, text, recorder))
    watcher = asyncio.create_task(_watch_cancel(deps, run.id))
    try:
        done, _ = await asyncio.wait({stream, watcher}, timeout=deps.run_time_limit_s, return_when=asyncio.FIRST_COMPLETED)
        if stream in done:
            exc = stream.exception()
            if exc is None:
                return Outcome("succeeded")
            return _failure(run.id, exc)
        stream.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await stream
        if watcher in done:
            return Outcome("cancelled")
        return Outcome("failed", "The run took too long and was stopped", "timeout")
    finally:
        for task in (stream, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(stream, watcher, return_exceptions=True)


def _failure(run_id: uuid.UUID, exc: BaseException) -> Outcome:
    # Type only: provider and tool errors can echo user content.
    logger.error("Run id=%s failed: %s", run_id, type(exc).__name__)
    if isinstance(exc, (UnknownModelError, ModelNotConfiguredError)):
        return Outcome("failed", "The selected model is not available", "model_unavailable")
    return Outcome("failed", "The run failed unexpectedly", "internal")


async def execute_run(deps: RunDeps, run_id: uuid.UUID) -> None:
    try:
        await _execute(deps, run_id)
    except Exception as exc:
        # Celery acks the task even when it raises, so nothing would redeliver it: without this, a run whose
        # setup or finalization failed would stay queued/running (409 for its conversation) with a hung stream.
        logger.error("Run id=%s task failed: %s", run_id, type(exc).__name__)
        await _fail_stuck_run(deps, run_id, exc)


async def _fail_stuck_run(deps: RunDeps, run_id: uuid.UUID, exc: Exception) -> None:
    try:
        loaded = await asyncio.to_thread(_load, deps, run_id)
        if loaded is None:
            return
        await _finish_run(deps, loaded[0], _failure(run_id, exc), TurnRecorder())
    except Exception as fallback_exc:
        # Database or Redis is down. The guards expire on their own and the DB check ignores stale runs.
        logger.error("Could not fail run id=%s: %s", run_id, type(fallback_exc).__name__)
        raise


async def _execute(deps: RunDeps, run_id: uuid.UUID) -> None:
    loaded = await asyncio.to_thread(_load, deps, run_id)
    if loaded is None:
        logger.warning("Run id=%s no longer exists", run_id)
        return
    run, model_id, text = loaded

    if run.status in TERMINAL_STATUSES:  # redelivered after it finished
        await _ensure_done_event(deps, run)
        return
    if not await asyncio.to_thread(_mark_running, deps, run_id):
        return

    recorder = TurnRecorder()
    outcome: Outcome | None = None
    try:
        if await deps.events_client.exists(cancel_key(run_id)):
            outcome = Outcome("cancelled")  # cancelled while queued
        else:
            outcome = await _run_to_outcome(deps, run, model_id, text, recorder)
    finally:
        # outcome is None only when this coroutine itself was cancelled (worker shutdown) or the setup above
        # raised: for the first, leave the run `running` with its guards so the broker's redelivery resumes it;
        # for the second, execute_run() fails the run.
        if outcome is not None:
            await _finish_run(deps, run, outcome, recorder)


PUBLISH_ATTEMPTS = 3


async def _publish_final(deps: RunDeps, run: Run, outcome: Outcome) -> None:
    """The `error`/`done` events end every client's stream, so retry them: the DB already says the run is over."""
    for attempt in range(PUBLISH_ATTEMPTS):
        try:
            if outcome.status == "failed" and attempt == 0:
                await events.publish(
                    deps.events_client, run.id, "error", {"message": outcome.error, "code": outcome.code}
                )
            await events.publish(deps.events_client, run.id, "done", {"status": outcome.status})
            await events.finish(deps.events_client, run.id)
            return
        except Exception as exc:
            logger.warning("Publishing the end of run id=%s failed: %s", run.id, type(exc).__name__)
            if attempt == PUBLISH_ATTEMPTS - 1:
                raise
            await asyncio.sleep(0.2 * 2**attempt)


async def _finish_run(deps: RunDeps, run: Run, outcome: Outcome, recorder: TurnRecorder) -> None:
    try:
        won = await asyncio.to_thread(_finalize_db, deps, run, outcome, recorder.content())
        if won:
            await _publish_final(deps, run, outcome)
    finally:
        await asyncio.to_thread(run_guard.release, deps.guard_client, run.user_id, run.conversation_id, run.id)


async def _ensure_done_event(deps: RunDeps, run: Run) -> None:
    """A previous delivery may have died between the DB update and the `done` event."""
    last = await deps.events_client.xrevrange(events.stream_key(run.id), count=1)
    if last and last[0][1].get("type") == "done":
        return
    await events.publish(deps.events_client, run.id, "done", {"status": run.status})
    await events.finish(deps.events_client, run.id)
    await asyncio.to_thread(run_guard.release, deps.guard_client, run.user_id, run.conversation_id, run.id)


async def reap_idle_forever(manager: SandboxManager, interval_s: float = 60.0) -> None:
    """Sandboxes idle past SANDBOX_IDLE_TIMEOUT_S live until their TTL otherwise (plan WP2 handoff)."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            reaped = await manager.reap_idle()
            if reaped:
                logger.info("Reaped %d idle sandbox(es)", reaped)
        except Exception as exc:
            logger.warning("Idle sandbox reaping failed: %s", type(exc).__name__)
