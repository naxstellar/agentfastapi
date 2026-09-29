import logging
import secrets
from typing import Annotated
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.auth.redis_state import consume_state, save_state
from app.auth.session_store import create_session, delete_session, get_session, update_session
from app.auth.token_refresh import apply_refreshed_tokens, refresh_tokens
from app.config import (
    AUTH0_AUDIENCE,
    AUTH0_CALLBACK_URL,
    AUTH0_CLIENT_ID,
    AUTH0_CLIENT_SECRET,
    AUTH0_DOMAIN,
    APP_BASE_URL,
)
from app.db import SessionLocal
from app.models import upsert_user

logger = logging.getLogger("app.auth")

router = APIRouter()


class RefreshResponse(BaseModel):
    refreshed: bool
    expires_in: int | None = None


class DecodedToken(BaseModel):
    header: dict
    payload: dict
    signature_verified: bool


class SessionInfo(BaseModel):
    authenticated: bool
    name: str | None = None
    email: str | None = None


def _authorize_url(state: str, screen_hint: str | None = None) -> str:
    params = {
        "response_type": "code",
        "client_id": AUTH0_CLIENT_ID,
        "redirect_uri": AUTH0_CALLBACK_URL,
        "scope": "openid profile email offline_access",
        "audience": AUTH0_AUDIENCE,
        "state": state,
    }
    if screen_hint:
        params["screen_hint"] = screen_hint
    return f"https://{AUTH0_DOMAIN}/authorize?{urlencode(params)}"


@router.get("/loginuser")
def login_user() -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    save_state(state)
    return RedirectResponse(_authorize_url(state))


@router.get("/register")
def register() -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    save_state(state)
    return RedirectResponse(_authorize_url(state, screen_hint="signup"))


@router.get("/callback")
def callback(
    request: Request,
    state: Annotated[str, Query()],
    code: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    if not consume_state(state):
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    if error:
        raise HTTPException(status_code=400, detail=f"{error}: {error_description}")

    if not code:
        raise HTTPException(status_code=400, detail="Missing 'code' parameter")

    token_response = httpx.post(
        f"https://{AUTH0_DOMAIN}/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": AUTH0_CLIENT_ID,
            "client_secret": AUTH0_CLIENT_SECRET,
            "code": code,
            "redirect_uri": AUTH0_CALLBACK_URL,
        },
    )
    if token_response.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {token_response.text}")

    tokens = token_response.json()

    claims = {}
    id_token = tokens.get("id_token")
    if id_token:
        claims = jwt.decode(id_token, options={"verify_signature": False})
        with SessionLocal() as db:
            user = upsert_user(db, claims)
        logger.info("Upserted user id=%s email=%s", user.id, user.email)

    session_id = create_session(
        {
            "access_token": tokens.get("access_token"),
            "id_token": id_token,
            "refresh_token": tokens.get("refresh_token"),
            "claims": {
                "name": claims.get("name"),
                "nickname": claims.get("nickname"),
                "email": claims.get("email"),
            },
        }
    )

    request.session["session_id"] = session_id
    return RedirectResponse(APP_BASE_URL)


@router.post("/refresh")
def refresh(request: Request) -> RefreshResponse:
    session_id = request.session.get("session_id")
    session = get_session(session_id)
    if not session or not session.get("refresh_token"):
        raise HTTPException(status_code=401, detail="No active session with a refresh token")

    try:
        tokens = refresh_tokens(session["refresh_token"])
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=400, detail=f"Refresh failed: {exc.response.text}") from exc

    update_session(session_id, apply_refreshed_tokens(session, tokens))

    return RefreshResponse(refreshed=True, expires_in=tokens.get("expires_in"))


@router.get("/decode-token")
def decode_token(token: Annotated[str, Query()]) -> DecodedToken:
    try:
        header = jwt.get_unverified_header(token)
        payload = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid token: {exc}") from exc
    return DecodedToken(header=header, payload=payload, signature_verified=False)


@router.get("/session-info", response_model_exclude_none=True)
def session_info(request: Request) -> SessionInfo:
    session_id = request.session.get("session_id")
    session = get_session(session_id)
    if not session:
        return SessionInfo(authenticated=False)
    claims = session.get("claims", {})
    return SessionInfo(
        authenticated=True,
        name=claims.get("name") or claims.get("nickname") or claims.get("email"),
        email=claims.get("email"),
    )


@router.get("/logout")
def logout(request: Request) -> RedirectResponse:
    session_id = request.session.get("session_id")
    delete_session(session_id)
    request.session.clear()
    params = {"client_id": AUTH0_CLIENT_ID, "returnTo": APP_BASE_URL}
    return RedirectResponse(f"https://{AUTH0_DOMAIN}/v2/logout?{urlencode(params)}")
