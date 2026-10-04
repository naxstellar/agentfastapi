"""Daily per-user token quota (plan §2.6).

Redis is the fast path (`quota:{user_id}:{YYYYMMDD}`, one counter per UTC day); Postgres `token_usage_daily` is
the record. A missing counter (expiry, Redis flush) is warmed from Postgres, which holds every *finished* run, so
a flush loses at most the tokens of runs still in flight; overshoot is accepted by design.

Per-run totals live in a Redis hash (`run:{id}:usage`) while the run is in flight, so a redelivered run keeps
counting where the first delivery stopped. `rollup_run()` writes them to `runs.*_tokens` and the daily row; it
only adds what the run row doesn't hold yet, so calling it twice for a run counts nothing twice.

Everything here is sync: async callers wrap it in `asyncio.to_thread`.
"""

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import redis
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import DAILY_TOKEN_BUDGET
from app.models_agent import Run, TokenUsageDaily

COUNTER_TTL_S = 48 * 3600
RUN_USAGE_TTL_S = 48 * 3600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today(now: datetime | None = None) -> date:
    return (now or _utcnow()).date()


def reset_at(now: datetime | None = None) -> datetime:
    """The next UTC midnight, when the day's counter starts over."""
    now = now or _utcnow()
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)


def counter_key(user_id: uuid.UUID, day: date) -> str:
    return f"quota:{user_id}:{day:%Y%m%d}"


def run_usage_key(run_id: uuid.UUID) -> str:
    return f"run:{run_id}:usage"


@dataclass(frozen=True)
class QuotaStatus:
    used: int
    limit: int
    reset_at: datetime

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exceeded(self) -> bool:
        return self.used >= self.limit

    @property
    def retry_after_s(self) -> int:
        return max(1, int((self.reset_at - _utcnow()).total_seconds()))


class Quota:
    """`session_factory` yields a DB session; it is only used to warm a missing counter."""

    def __init__(self, client: redis.Redis, session_factory: Callable[[], AbstractContextManager[Session]]):
        self._client = client
        self._session_factory = session_factory

    def _used(self, user_id: uuid.UUID, day: date) -> int:
        key = counter_key(user_id, day)
        value = self._client.get(key)
        if value is None:
            with self._session_factory() as db:
                recorded = db.scalar(
                    select(TokenUsageDaily.input_tokens + TokenUsageDaily.output_tokens).where(
                        TokenUsageDaily.user_id == user_id, TokenUsageDaily.day == day
                    )
                )
            # NX: a concurrent add_usage may have created the counter meanwhile; it must not be overwritten.
            self._client.set(key, int(recorded or 0), nx=True, ex=COUNTER_TTL_S)
            value = self._client.get(key)
        return int(value or 0)

    def status(self, user_id: uuid.UUID) -> QuotaStatus:
        now = _utcnow()
        return QuotaStatus(self._used(user_id, today(now)), DAILY_TOKEN_BUDGET, reset_at(now))

    def add_usage(self, user_id: uuid.UUID, run_id: uuid.UUID, input_tokens: int, output_tokens: int) -> tuple[int, int, int]:
        """Counts one LLM call. Returns the run's totals so far and the user's day total."""
        day = today()
        self._used(user_id, day)  # warm
        key, run_key = counter_key(user_id, day), run_usage_key(run_id)
        with self._client.pipeline(transaction=True) as pipe:
            pipe.incrby(key, input_tokens + output_tokens)
            pipe.expire(key, COUNTER_TTL_S)
            pipe.hincrby(run_key, "input", input_tokens)
            pipe.hincrby(run_key, "output", output_tokens)
            pipe.expire(run_key, RUN_USAGE_TTL_S)
            day_used, _, run_in, run_out, _ = pipe.execute()
        return int(run_in), int(run_out), int(day_used)

    def run_usage(self, run_id: uuid.UUID) -> tuple[int, int]:
        values = self._client.hgetall(run_usage_key(run_id))
        return int(values.get("input", 0)), int(values.get("output", 0))


def rollup_run(db: Session, run_id: uuid.UUID, user_id: uuid.UUID, input_tokens: int, output_tokens: int) -> None:
    """Records a run's tokens on its row and in the user's daily total, in the caller's transaction.

    Idempotent: only the part the run row doesn't hold yet is added to the daily row.
    """
    row = db.execute(
        select(Run.input_tokens, Run.output_tokens).where(Run.id == run_id).with_for_update()
    ).one_or_none()
    if row is None:
        return
    delta_in = max(0, input_tokens - row.input_tokens)
    delta_out = max(0, output_tokens - row.output_tokens)
    if not (delta_in or delta_out):
        return
    db.execute(
        update(Run)
        .where(Run.id == run_id)
        .values(input_tokens=row.input_tokens + delta_in, output_tokens=row.output_tokens + delta_out)
        .execution_options(synchronize_session=False)
    )
    stmt = pg_insert(TokenUsageDaily).values(
        user_id=user_id, day=today(), input_tokens=delta_in, output_tokens=delta_out
    )
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[TokenUsageDaily.user_id, TokenUsageDaily.day],
            set_={
                "input_tokens": TokenUsageDaily.input_tokens + stmt.excluded.input_tokens,
                "output_tokens": TokenUsageDaily.output_tokens + stmt.excluded.output_tokens,
            },
        )
    )
