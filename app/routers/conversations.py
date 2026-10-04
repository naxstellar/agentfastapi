"""Conversations CRUD and display history (WP7)."""

import base64
import binascii
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, delete, or_, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.agent.models import resolve_model
from app.agent.sandbox import SandboxUnavailableError, delete_sandbox_sync
from app.auth.security import DbUserDep
from app.config import ALLOWED_MODELS
from app.db import DbSessionDep
from app.models import User
from app.models_agent import Artifact, Conversation, Message, Run, UserSettings
from app.redis_client import redis_client
from app.services.storage import StorageNotConfiguredError, get_storage
from app.workers.runner import ACTIVE_STATUSES

router = APIRouter(tags=["conversations"])

MAX_TITLE_CHARS = 200
AUTO_TITLE_CHARS = 60
# LangGraph's checkpointer tables; every row of a conversation has thread_id = conversation id.
CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")

Title = Annotated[str, Field(min_length=1, max_length=MAX_TITLE_CHARS)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    model: str
    created_at: datetime
    updated_at: datetime


class ConversationPage(BaseModel):
    items: list[ConversationOut]
    next_cursor: str | None  # pass as `cursor` for the next page; None at the end


class ConversationCreate(BaseModel):
    title: Title | None = None
    model: str | None = None  # default: the user's default_model setting, else DEFAULT_MODEL


class ConversationPatch(BaseModel):
    title: Title | None = None
    model: str | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: dict[str, Any]
    created_at: datetime


class MessagePage(BaseModel):
    items: list[MessageOut]  # oldest first
    next_before: int | None  # pass as `before` to load the page of older messages; None at the start


def auto_title(text_: str) -> str:
    """First line of the first message, cut to a short title."""
    line = " ".join(text_.split())
    return line if len(line) <= AUTO_TITLE_CHARS else line[: AUTO_TITLE_CHARS - 1].rstrip() + "…"


def _check_model(model: str) -> str:
    if model not in ALLOWED_MODELS:
        raise HTTPException(status_code=422, detail=f"model must be one of {', '.join(ALLOWED_MODELS)}")
    return model


def _owned(db: Session, user: User, conversation_id: uuid.UUID, *, lock: bool = False) -> Conversation:
    query = select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id)
    if lock:
        query = query.with_for_update()
    conversation = db.scalars(query).one_or_none()
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


def _has_active_run(db: Session, conversation_id: uuid.UUID) -> bool:
    return db.scalar(select(Run.id).where(Run.conversation_id == conversation_id, Run.status.in_((*ACTIVE_STATUSES, "awaiting_approval"))).limit(1)) is not None


def _encode_cursor(conversation: Conversation) -> str:
    # URL-safe: an ISO timestamp's "+00:00" would turn into a space in a query string.
    return base64.urlsafe_b64encode(f"{conversation.created_at.isoformat()}_{conversation.id}".encode()).decode()


def _parse_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        stamp, raw_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("_", 1)
        return datetime.fromisoformat(stamp), uuid.UUID(raw_id)
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=400, detail="Malformed cursor") from None


@router.post("/conversations", status_code=201)
def create_conversation(body: ConversationCreate, db: DbSessionDep, user: DbUserDep) -> ConversationOut:
    if body.model is not None:
        model = _check_model(body.model)
    else:
        settings = db.get(UserSettings, user.id)
        model = resolve_model(settings.default_model if settings and settings.default_model in ALLOWED_MODELS else None)
    conversation = Conversation(user_id=user.id, title=body.title, model=model)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return ConversationOut.model_validate(conversation)


@router.get("/conversations")
def list_conversations(
    db: DbSessionDep, user: DbUserDep, limit: PageSize = 30, cursor: str | None = None
) -> ConversationPage:
    # Newest first by (created_at, id): neither changes, so pages don't shift when conversations are renamed or used.
    query = select(Conversation).where(Conversation.user_id == user.id)
    if cursor is not None:
        created_at, last_id = _parse_cursor(cursor)
        query = query.where(
            or_(
                Conversation.created_at < created_at,
                and_(Conversation.created_at == created_at, Conversation.id < last_id),
            )
        )
    rows = db.scalars(query.order_by(Conversation.created_at.desc(), Conversation.id.desc()).limit(limit + 1)).all()
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1]) if len(rows) > limit else None
    return ConversationPage(items=[ConversationOut.model_validate(c) for c in page], next_cursor=next_cursor)


@router.get("/conversations/{conversation_id}")
def read_conversation(conversation_id: uuid.UUID, db: DbSessionDep, user: DbUserDep) -> ConversationOut:
    return ConversationOut.model_validate(_owned(db, user, conversation_id))


@router.patch("/conversations/{conversation_id}")
def update_conversation(
    conversation_id: uuid.UUID, body: ConversationPatch, db: DbSessionDep, user: DbUserDep
) -> ConversationOut:
    conversation = _owned(db, user, conversation_id, lock=True)
    if body.title is not None:
        conversation.title = body.title
    if body.model is not None and body.model != conversation.model:
        if _has_active_run(db, conversation.id):
            raise HTTPException(status_code=409, detail="Can't change the model while a run is in progress")
        conversation.model = _check_model(body.model)
    db.commit()
    db.refresh(conversation)
    return ConversationOut.model_validate(conversation)


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: uuid.UUID, db: DbSessionDep, user: DbUserDep) -> Response:
    """Removes the conversation, its messages and runs, checkpoint state, sandbox and artifact files.
    Datasets are the user's own and survive; only their attachment goes (by cascade).

    External state goes first, so a failure leaves the row and the delete can be retried.
    """
    conversation = _owned(db, user, conversation_id, lock=True)
    if _has_active_run(db, conversation.id):
        raise HTTPException(status_code=409, detail="This conversation has a run in progress; cancel it first")

    keys = db.scalars(
        select(Artifact.gcs_key).join(Run, Artifact.run_id == Run.id).where(Run.conversation_id == conversation.id)
    ).all()
    if keys:
        try:
            storage = get_storage()
            for key in keys:
                storage.delete(key)
        except StorageNotConfiguredError as exc:
            raise HTTPException(status_code=503, detail="File storage is not configured") from exc
    if conversation.sandbox_id:
        try:
            delete_sandbox_sync(conversation.sandbox_id, redis_client)
        except SandboxUnavailableError as exc:
            raise HTTPException(status_code=503, detail="The code sandbox could not be deleted; try again") from exc

    for table in CHECKPOINT_TABLES:
        try:
            with db.begin_nested():
                db.execute(text(f"DELETE FROM {table} WHERE thread_id = :t"), {"t": str(conversation.id)})
        except ProgrammingError:  # the worker creates these on first start; no table, nothing to delete
            pass
    db.execute(delete(Conversation).where(Conversation.id == conversation.id))  # messages, runs, links cascade
    db.commit()
    return Response(status_code=204)


@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: uuid.UUID, db: DbSessionDep, user: DbUserDep, limit: PageSize = 50, before: int | None = None
) -> MessagePage:
    """The newest `limit` messages (older ones with `before`), returned oldest first. Ids only grow, so a page
    never shifts when new messages arrive."""
    _owned(db, user, conversation_id)
    query = select(Message).where(Message.conversation_id == conversation_id)
    if before is not None:
        query = query.where(Message.id < before)
    rows = db.scalars(query.order_by(Message.id.desc()).limit(limit + 1)).all()
    page = rows[:limit]
    next_before = page[-1].id if len(rows) > limit else None
    return MessagePage(items=[MessageOut.model_validate(m) for m in reversed(page)], next_before=next_before)
