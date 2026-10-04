"""Code approvals (WP6C): resuming a run that paused for the user's decision, and expiring the ones nobody answers.

A paused run is `awaiting_approval` in Postgres, with its graph state in the checkpoint. Resuming re-enqueues the
same run id with the decision as a task argument; the run isn't quota-gated (it is the same in-flight run). The
deadline of each pause lives in a Redis sorted set that the periodic `agent.expire_approvals` task reads; a Redis
flush loses deadlines, and the task re-registers every paused run it doesn't find there.
"""

import logging
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import Enum

import redis
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import APPROVAL_TIMEOUT_S
from app.models_agent import Run
from app.services import run_guard

logger = logging.getLogger("app.services.approvals")

PENDING_KEY = "approvals:pending"
TIMEOUT_MESSAGE = "Nobody answered in time, so the code was not run."


class Resume(Enum):
    RESUMED = "resumed"
    NOT_WAITING = "not_waiting"  # already answered, cancelled, or never paused
    CONVERSATION_BUSY = "conversation_busy"
    USER_AT_LIMIT = "user_at_limit"


def register_pending(client: redis.Redis, run_id: uuid.UUID) -> None:
    client.zadd(PENDING_KEY, {str(run_id): time.time() + APPROVAL_TIMEOUT_S}, nx=True)


def resume_run(
    db: Session, client: redis.Redis, run: Run, decision: dict | None, *, take_guards: bool = True
) -> Resume:
    """awaiting_approval -> queued and enqueues the run with the user's decision (None: just finish it, used when
    the user cancels a paused run). The caller must have checked ownership."""
    from app.workers.tasks import run_agent  # the worker module imports this one

    if db.scalar(select(Run.status).where(Run.id == run.id)) != "awaiting_approval":
        db.rollback()  # end the read's transaction
        return Resume.NOT_WAITING  # answered already: don't touch the guards its resumed run holds

    if take_guards:
        admission = run_guard.acquire(client, run.user_id, run.conversation_id, run.id)
        if admission is run_guard.Admission.CONVERSATION_BUSY:
            return Resume.CONVERSATION_BUSY
        if admission is run_guard.Admission.USER_AT_LIMIT:
            return Resume.USER_AT_LIMIT

    def release() -> None:
        if take_guards:
            run_guard.release(client, run.user_id, run.conversation_id, run.id)

    try:
        result = db.execute(
            update(Run).where(Run.id == run.id, Run.status == "awaiting_approval").values(status="queued")
        )
        db.commit()
    except BaseException:
        db.rollback()
        release()
        raise
    if result.rowcount != 1:  # someone else answered first
        release()
        return Resume.NOT_WAITING
    client.zrem(PENDING_KEY, str(run.id))
    try:
        run_agent.apply_async(args=[str(run.id), decision], task_id=str(run.id))
    except Exception:
        release()
        db.execute(update(Run).where(Run.id == run.id, Run.status == "queued").values(status="awaiting_approval"))
        db.commit()
        register_pending(client, run.id)
        raise
    return Resume.RESUMED


def expire_approvals(session_factory: Callable[[], AbstractContextManager[Session]], client: redis.Redis) -> int:
    """Rejects the paused runs whose deadline passed; returns how many were resumed."""
    resumed = 0
    with session_factory() as db:
        # Paused runs missing from the set (Redis was flushed) get a fresh deadline.
        for run_id in db.scalars(select(Run.id).where(Run.status == "awaiting_approval")):
            register_pending(client, run_id)
        for raw in client.zrangebyscore(PENDING_KEY, "-inf", time.time()):
            run = db.get(Run, uuid.UUID(raw))
            if run is None or run.status != "awaiting_approval":
                client.zrem(PENDING_KEY, raw)
                continue
            outcome = resume_run(db, client, run, {"type": "reject", "message": TIMEOUT_MESSAGE})
            if outcome is Resume.RESUMED:
                resumed += 1
            elif outcome is Resume.NOT_WAITING:
                client.zrem(PENDING_KEY, raw)
            # busy / at the user's limit: leave it, the next tick tries again
    return resumed
