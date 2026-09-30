import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db import Base
from app.models import _utcnow

RUN_STATUSES = ("queued", "running", "awaiting_approval", "succeeded", "failed", "cancelled")
DATASET_STATUSES = ("pending", "ready", "rejected")


def _in_check(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


def _user_fk() -> ForeignKey:
    return ForeignKey("users.id", ondelete="CASCADE")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), _user_fk(), nullable=False, index=True)
    title = Column(String, nullable=True)
    model = Column(String, nullable=False)
    sandbox_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_id_id", "conversation_id", "id"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id = Column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role = Column(String, nullable=False)
    content = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (_in_check("status", RUN_STATUSES, "ck_runs_status"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = Column(UUID(as_uuid=True), _user_fk(), nullable=False, index=True)
    status = Column(String, nullable=False, default="queued")
    celery_task_id = Column(String, nullable=True)
    input_tokens = Column(BigInteger, nullable=False, default=0)
    output_tokens = Column(BigInteger, nullable=False, default=0)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (_in_check("status", DATASET_STATUSES, "ck_datasets_status"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), _user_fk(), nullable=False, index=True)
    filename = Column(String, nullable=False)
    gcs_key = Column(String, nullable=False, unique=True)
    size_bytes = Column(BigInteger, nullable=False)
    content_type = Column(String, nullable=False)
    sheets = Column(JSONB, nullable=True)
    status = Column(String, nullable=False, default="pending")
    # Not in plan §2.3; added now because WP1 T1.4 records a rejection reason and create_all can't add columns later.
    reject_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ConversationDataset(Base):
    __tablename__ = "conversation_datasets"

    conversation_id = Column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    dataset_id = Column(
        UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), _user_fk(), nullable=False, index=True)
    kind = Column(String, nullable=False)
    gcs_key = Column(String, nullable=False, unique=True)
    filename = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class TokenUsageDaily(Base):
    __tablename__ = "token_usage_daily"

    user_id = Column(UUID(as_uuid=True), _user_fk(), primary_key=True)
    day = Column(Date, primary_key=True)
    input_tokens = Column(BigInteger, nullable=False, default=0)
    output_tokens = Column(BigInteger, nullable=False, default=0)


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id = Column(UUID(as_uuid=True), _user_fk(), primary_key=True)
    require_code_approval = Column(Boolean, nullable=False, default=False)
    default_model = Column(String, nullable=True)
