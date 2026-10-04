"""GET/DELETE /memories: what the agent remembers about the signed-in user (WP6A).

The agent writes through the LangGraph store (async, in the worker). The API reads the store's table with plain
SQL instead, so it needs no async connection pool (psycopg's async mode doesn't work on Windows' default loop).
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.agent.memory import memory_namespace
from app.auth.security import DbUserDep
from app.config import MAX_MEMORIES
from app.db import DbSessionDep

router = APIRouter(tags=["memories"])


class MemoryOut(BaseModel):
    id: str
    text: str
    created_at: datetime


def _prefix(user) -> str:
    return ".".join(memory_namespace(user.id))  # how the store joins a namespace


@router.get("/memories")
def list_memories(db: DbSessionDep, user: DbUserDep) -> list[MemoryOut]:
    try:
        rows = db.execute(
            text(
                "SELECT key, value->>'text' AS text, created_at FROM store "
                "WHERE prefix = :prefix ORDER BY updated_at DESC LIMIT :limit"
            ),
            {"prefix": _prefix(user), "limit": MAX_MEMORIES},
        ).all()
    except ProgrammingError:  # the worker creates the store's tables on first start; no table, no memories
        db.rollback()
        return []
    return [MemoryOut(id=r.key, text=r.text or "", created_at=r.created_at) for r in rows]


@router.delete("/memories/{memory_id}", status_code=204)
def delete_memory(memory_id: str, db: DbSessionDep, user: DbUserDep) -> Response:
    try:
        result = db.execute(
            text("DELETE FROM store WHERE prefix = :prefix AND key = :key"), {"prefix": _prefix(user), "key": memory_id}
        )
    except ProgrammingError:
        db.rollback()
        raise HTTPException(404, "Memory not found")
    if result.rowcount != 1:
        raise HTTPException(404, "Memory not found")
    db.commit()
    return Response(status_code=204)
