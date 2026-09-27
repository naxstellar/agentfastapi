import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Session

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    auth0_sub = Column(String, unique=True, nullable=False, index=True)
    email = Column(String, nullable=True)
    name = Column(String, nullable=True)
    picture = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


def upsert_user(db: Session, claims: dict) -> User:
    sub = claims.get("sub")
    user = db.query(User).filter(User.auth0_sub == sub).one_or_none()
    if user is None:
        user = User(auth0_sub=sub)
        db.add(user)

    user.email = claims.get("email")
    user.name = claims.get("name")
    user.picture = claims.get("picture")

    db.commit()
    db.refresh(user)
    return user
