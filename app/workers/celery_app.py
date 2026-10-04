from celery import Celery

from app.config import CELERY_BROKER_URL, RUN_TIME_LIMIT_S

celery_app = Celery("agentfastapi", broker=CELERY_BROKER_URL, include=["app.workers.datasets", "app.workers.tasks"])
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    # Needs a beat process (scripts/start-beat.ps1): auto-rejects code approvals nobody answered.
    beat_schedule={"expire-approvals": {"task": "agent.expire_approvals", "schedule": 60.0}},
    broker_connection_retry_on_startup=True,
    # Must exceed the longest task, or Redis re-delivers it to a second worker while it is still running.
    broker_transport_options={"visibility_timeout": RUN_TIME_LIMIT_S + 600},
)
