"""GET/PUT /settings: the signed-in user's agent preferences (WP6C)."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.auth.security import DbUserDep
from app.config import ALLOWED_MODELS
from app.db import DbSessionDep
from app.models_agent import UserSettings

router = APIRouter(tags=["settings"])


class SettingsOut(BaseModel):
    require_code_approval: bool = False
    default_model: str | None = None
    allowed_models: list[str] = []  # read-only: what default_model (and a conversation's model) may be


class SettingsIn(BaseModel):
    require_code_approval: bool
    default_model: str | None = None


def _out(row: UserSettings | None) -> SettingsOut:
    return SettingsOut(
        require_code_approval=row.require_code_approval if row else False,
        default_model=row.default_model if row else None,
        allowed_models=list(ALLOWED_MODELS),
    )


@router.get("/settings")
def get_settings(db: DbSessionDep, user: DbUserDep) -> SettingsOut:
    row = db.get(UserSettings, user.id)
    return _out(row)


@router.put("/settings")
def put_settings(body: SettingsIn, db: DbSessionDep, user: DbUserDep) -> SettingsOut:
    if body.default_model is not None and body.default_model not in ALLOWED_MODELS:
        raise HTTPException(status_code=422, detail=f"default_model must be one of {', '.join(ALLOWED_MODELS)}")
    row = db.get(UserSettings, user.id)
    if row is None:
        row = UserSettings(user_id=user.id)
        db.add(row)
    row.require_code_approval = body.require_code_approval
    row.default_model = body.default_model
    db.commit()
    return _out(row)
