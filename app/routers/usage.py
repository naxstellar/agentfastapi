from contextlib import nullcontext
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app.auth.security import DbUserDep
from app.db import DbSessionDep
from app.redis_client import redis_client
from app.services.quota import Quota

router = APIRouter(tags=["usage"])


class UsageOut(BaseModel):
    used: int
    limit: int
    remaining: int
    reset_at: datetime


@router.get("/usage")
def get_usage(db: DbSessionDep, user: DbUserDep) -> UsageOut:
    status = Quota(redis_client, lambda: nullcontext(db)).status(user.id)
    return UsageOut(used=status.used, limit=status.limit, remaining=status.remaining, reset_at=status.reset_at)
