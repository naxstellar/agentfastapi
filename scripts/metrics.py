"""Prints basic operational numbers: queue depth, active runs, runs and tokens today, run durations.

Needs the same env as the app (env/.env). Usage: .venv/Scripts/python.exe scripts/metrics.py [--json]
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import redis  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.config import CELERY_BROKER_URL, DAILY_TOKEN_BUDGET  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models_agent import Run, TokenUsageDaily  # noqa: E402


def collect() -> dict:
    broker = redis.Redis.from_url(CELERY_BROKER_URL)
    today = datetime.now(timezone.utc).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    with SessionLocal() as db:
        by_status = dict(db.execute(select(Run.status, func.count()).group_by(Run.status)).all())
        tokens = db.execute(
            select(func.coalesce(func.sum(TokenUsageDaily.input_tokens), 0), func.coalesce(func.sum(TokenUsageDaily.output_tokens), 0))
            .where(TokenUsageDaily.day == today)
        ).one()
        seconds = db.scalars(
            select(func.extract("epoch", Run.finished_at - Run.started_at))
            .where(Run.finished_at >= start, Run.started_at.is_not(None))
            .order_by(func.extract("epoch", Run.finished_at - Run.started_at))
        ).all()
    p95 = float(seconds[min(len(seconds) - 1, int(len(seconds) * 0.95))]) if seconds else None
    return {
        "queue_depth": broker.llen("celery"),  # the default Celery queue (a Redis list)
        "runs_by_status": by_status,
        "tokens_today": {"input": int(tokens[0]), "output": int(tokens[1]), "daily_budget_per_user": DAILY_TOKEN_BUDGET},
        "runs_finished_today": len(seconds),
        "run_duration_s_p95_today": p95,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    metrics = collect()
    print(json.dumps(metrics, indent=None if parser.parse_args().json else 2, default=str))
