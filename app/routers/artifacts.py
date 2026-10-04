"""GET /artifacts/{id}: a short-lived signed download URL for a file the agent produced (WP7)."""

import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.auth.security import DbUserDep
from app.db import DbSessionDep
from app.models_agent import Artifact
from app.routers.datasets import SignedUrlOut, StorageDep, _signed

router = APIRouter(tags=["artifacts"])


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: uuid.UUID, db: DbSessionDep, user: DbUserDep, storage: StorageDep) -> SignedUrlOut:
    artifact = db.scalar(select(Artifact).where(Artifact.id == artifact_id, Artifact.user_id == user.id))
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return _signed(storage.download_url(artifact.gcs_key, artifact.filename), "GET")
