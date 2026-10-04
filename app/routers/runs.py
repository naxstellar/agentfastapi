import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Annotated

import anyio.to_thread
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agent.history import add_user_message
from app.auth.security import DbUserDep
from app.db import DbSessionDep
from app.models_agent import Conversation, Run
from app.redis_client import redis_client
from app.services import events, run_guard
from app.workers.runner import ACTIVE_STATUSES, CANCEL_FLAG_TTL_S, TERMINAL_STATUSES, cancel_key
from app.workers.tasks import run_agent

router = APIRouter(tags=["runs"])

# The newest message is always kept verbatim by summarization, so bound what one turn can add to the context.
MAX_MESSAGE_CHARS = 20_000
STREAM_ID = re.compile(r"\d+(-\d+)?")


class RunCreate(BaseModel):
    text: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_CHARS)]


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    status: str
    error: str | None
    input_tokens: int
    output_tokens: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _owned_run(db: Session, user_id: uuid.UUID, run_id: uuid.UUID) -> Run:
    run = db.scalar(select(Run).where(Run.id == run_id, Run.user_id == user_id))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.post("/conversations/{conversation_id}/runs", status_code=202)
def create_run(conversation_id: uuid.UUID, body: RunCreate, db: DbSessionDep, user: DbUserDep) -> RunOut:
    conversation = db.scalar(
        select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id)
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    run_id = uuid.uuid4()
    admission = run_guard.acquire(redis_client, user.id, conversation_id, run_id)
    if admission is run_guard.Admission.CONVERSATION_BUSY:
        raise HTTPException(status_code=409, detail="This conversation already has a run in progress")
    if admission is run_guard.Admission.USER_AT_LIMIT:
        raise HTTPException(status_code=429, detail="Too many runs in progress", headers={"Retry-After": "10"})

    try:
        # The Redis lock is advisory (it can expire or be flushed); the DB is the check that can't be lost.
        # Runs older than the guard TTL are ignored: a run whose worker and task both died would otherwise block
        # its conversation forever.
        stale_before = datetime.now(timezone.utc) - timedelta(seconds=run_guard.GUARD_TTL_S)
        if db.scalar(
            select(Run.id)
            .where(Run.conversation_id == conversation_id, Run.status.in_(ACTIVE_STATUSES), Run.created_at > stale_before)
            .limit(1)
        ):
            raise HTTPException(status_code=409, detail="This conversation already has a run in progress")
        run = Run(id=run_id, conversation_id=conversation_id, user_id=user.id, status="queued", celery_task_id=str(run_id))
        db.add(run)
        add_user_message(db, conversation_id, run_id, body.text)
        db.commit()
    except BaseException:
        db.rollback()
        run_guard.release(redis_client, user.id, conversation_id, run_id)
        raise

    try:
        run_agent.apply_async(args=[str(run_id)], task_id=str(run_id))
    except Exception:
        run_guard.release(redis_client, user.id, conversation_id, run_id)
        db.execute(
            update(Run).where(Run.id == run_id).values(status="failed", error="The run could not be queued")
        )
        db.commit()
        raise
    return RunOut.model_validate(run)


@router.get("/runs/{run_id}")
def get_run(run_id: uuid.UUID, db: DbSessionDep, user: DbUserDep) -> RunOut:
    return RunOut.model_validate(_owned_run(db, user.id, run_id))


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: uuid.UUID, db: DbSessionDep, user: DbUserDep) -> RunOut:
    run = await anyio.to_thread.run_sync(_owned_run, db, user.id, run_id)
    if run.status in ACTIVE_STATUSES:
        # The worker's watcher (or its start-up check, for a queued run) acts on this flag.
        await events.api_events_client().set(cancel_key(run_id), "1", ex=CANCEL_FLAG_TTL_S)
    return RunOut.model_validate(run)


@dataclass(frozen=True)
class StreamRequest:
    run_id: uuid.UUID
    start: str  # Redis stream id to read after
    final_status: Callable[[], str | None]  # the run's status once it has ended, else None (sync: DB)


async def authorize_stream(
    run_id: uuid.UUID,
    db: DbSessionDep,
    user: DbUserDep,
    last_event_id: Annotated[str | None, Header()] = None,
    from_id: Annotated[str | None, Query(alias="from")] = None,
) -> StreamRequest:
    """Runs before the response starts, so failures are real 4xx responses (an exception raised inside the
    SSE generator can't change the status line)."""
    start = last_event_id or from_id or "0"
    if not STREAM_ID.fullmatch(start):
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID")

    def load() -> str:
        status = _owned_run(db, user.id, run_id).status
        db.commit()  # end the transaction: the connection must not be held for the stream's lifetime
        return status

    status = await anyio.to_thread.run_sync(load)
    if status in TERMINAL_STATUSES and not await events.api_events_client().exists(events.stream_key(run_id)):
        raise HTTPException(status_code=410, detail="The run's event stream has expired; read the messages instead")
    def final_status() -> str | None:
        current = db.scalar(select(Run.status).where(Run.id == run_id))
        db.commit()
        return current if current in TERMINAL_STATUSES else None

    return StreamRequest(run_id, start, final_status)


@router.get("/runs/{run_id}/events", response_class=EventSourceResponse)
async def stream_run_events(
    request: Annotated[StreamRequest, Depends(authorize_stream)], response: Response
) -> AsyncIterator[ServerSentEvent]:
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Cache-Control"] = "no-cache"
    async for item in events.tail(events.api_events_client(), request.run_id, request.start):
        if item is None:
            # Idle: if the run ended but its `done` never reached the stream (a failed publish), close it here.
            if (ended := await anyio.to_thread.run_sync(request.final_status)) is not None:
                yield ServerSentEvent(event="done", data={"status": ended})
                return
            yield ServerSentEvent(comment="keep-alive")
            continue
        entry_id, event_type, data = item
        yield ServerSentEvent(event=event_type, id=entry_id, data=data)
