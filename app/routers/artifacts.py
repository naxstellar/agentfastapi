"""GET /artifacts/{id}: a short-lived signed download URL for a file the agent produced (WP7), and
GET /conversations/{id}/artifacts: everything a conversation produced (WP8A)."""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.auth.security import DbUserDep
from app.db import DbSessionDep
from app.models_agent import Artifact, Conversation, Run
from app.routers.datasets import SignedUrlOut, StorageDep, _signed

router = APIRouter(tags=["artifacts"])


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: uuid.UUID, db: DbSessionDep, user: DbUserDep, storage: StorageDep) -> SignedUrlOut:
    artifact = db.scalar(select(Artifact).where(Artifact.id == artifact_id, Artifact.user_id == user.id))
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return _signed(storage.download_url(artifact.gcs_key, artifact.filename), "GET")


class ArtifactOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    kind: str
    filename: str
    created_at: datetime


@router.get("/conversations/{conversation_id}/artifacts")
def list_conversation_artifacts(conversation_id: uuid.UUID, db: DbSessionDep, user: DbUserDep) -> list[ArtifactOut]:
    owned = db.scalar(select(Conversation.id).where(Conversation.id == conversation_id, Conversation.user_id == user.id))
    if owned is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    rows = db.scalars(
        select(Artifact)
        .join(Run, Run.id == Artifact.run_id)
        .where(Run.conversation_id == conversation_id, Artifact.user_id == user.id)
        .order_by(Artifact.created_at.desc(), Artifact.id)
    ).all()
    return [ArtifactOut(id=a.id, run_id=a.run_id, kind=a.kind, filename=a.filename, created_at=a.created_at) for a in rows]
