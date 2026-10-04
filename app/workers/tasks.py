"""Celery task for agent runs (plan §2.8).

The threads pool never forks, so `worker_process_init` doesn't fire; the worker's one long-lived event loop
(in a background thread) and its async resources are created lazily by the first task instead. Tasks submit
the run coroutine to that loop and wait on it, so every run shares one Postgres pool and one set of clients.
"""

import asyncio
import logging
import threading
import uuid
from concurrent.futures import Future

import redis.asyncio
from sqlalchemy.orm import sessionmaker

from app.agent.graph import open_persistence
from app.agent.sandbox import make_sandbox_manager
from app.config import REDIS_URL
from app.db import SessionLocal
from app.redis_client import redis_client
from app.services import approvals
from app.services.events import make_events_client
from app.services.storage import get_storage
from app.workers.celery_app import celery_app
from app.workers.runner import RunDeps, execute_run, reap_idle_forever

logger = logging.getLogger("app.workers.tasks")


class WorkerRuntime:
    """An event loop on its own thread, plus the async resources that live on it."""

    def __init__(self, session_factory: sessionmaker = SessionLocal):
        self._session_factory = session_factory
        self._loop = asyncio.SelectorEventLoop()  # psycopg's async mode doesn't support Windows' Proactor loop
        self._thread = threading.Thread(target=self._run_loop, name="agent-loop", daemon=True)
        self._deps: RunDeps | None = None
        self._ready: Future[None] = Future()
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._start(), self._loop)
        self._ready.result()  # raises if resources couldn't be created

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _start(self) -> None:
        try:
            persistence_cm = open_persistence()
            persistence = await persistence_cm.__aenter__()
            self._persistence_cm = persistence_cm
            async_redis = redis.asyncio.Redis.from_url(REDIS_URL, decode_responses=True)
            manager = make_sandbox_manager(async_redis)
            self._deps = RunDeps(
                persistence=persistence,
                manager=manager,
                storage=get_storage(),
                events_client=make_events_client(),
                guard_client=redis_client,
                session_factory=self._session_factory,
            )
            self._reaper = asyncio.create_task(reap_idle_forever(manager))
            self._ready.set_result(None)
        except BaseException as exc:
            self._ready.set_exception(exc)

    def run(self, run_id: uuid.UUID, decision: dict | None = None) -> None:
        assert self._deps is not None
        asyncio.run_coroutine_threadsafe(execute_run(self._deps, run_id, decision), self._loop).result()


_runtime: WorkerRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> WorkerRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = WorkerRuntime()
        return _runtime


@celery_app.task(name="agent.run")
def run_agent(run_id: str, decision: dict | None = None) -> None:
    """`decision` is the user's answer when this resumes a run paused for code approval (WP6C)."""
    get_runtime().run(uuid.UUID(run_id), decision)


@celery_app.task(name="agent.expire_approvals")
def expire_approvals_task() -> int:
    """Periodic (celery beat): rejects paused runs nobody answered within APPROVAL_TIMEOUT_S."""
    return approvals.expire_approvals(SessionLocal, redis_client)
