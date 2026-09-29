from typing import Annotated

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from jwt import PyJWKClient

from app.auth.session_store import get_session, update_session
from app.auth.token_refresh import apply_refreshed_tokens, refresh_tokens
from app.config import AUTH0_AUDIENCE, AUTH0_ISSUER, AUTH0_JWKS_URL

_jwks_client = PyJWKClient(AUTH0_JWKS_URL)


def _decode_access_token(token: str) -> dict:
    signing_key = _jwks_client.get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=AUTH0_AUDIENCE,
        issuer=AUTH0_ISSUER,
    )


def get_current_user(request: Request) -> dict:
    session_id = request.session.get("session_id")
    session = get_session(session_id)
    if not session or not session.get("access_token"):
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        return _decode_access_token(session["access_token"])
    except jwt.ExpiredSignatureError:
        pass
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc

    refresh_token = session.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Session expired; please log in again")

    try:
        tokens = refresh_tokens(refresh_token)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=401, detail="Session expired; please log in again") from exc

    update_session(session_id, apply_refreshed_tokens(session, tokens))

    try:
        return _decode_access_token(session["access_token"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid refreshed token: {exc}") from exc


CurrentUserDep = Annotated[dict, Depends(get_current_user)]


def require_permission(permission: str):
    def _check(user: CurrentUserDep) -> dict:
        if permission not in user.get("permissions", []):
            raise HTTPException(status_code=403, detail=f"Missing permission: {permission}")
        return user

    return _check
