import time
from typing import Annotated

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from jwt import PyJWKClient

from app.auth.session_store import (
    REFRESH_LOCK_TTL_SECONDS,
    acquire_refresh_lock,
    get_session,
    refresh_lock_held,
    release_refresh_lock,
    update_session,
)
from app.auth.token_refresh import apply_refreshed_tokens, refresh_tokens
from app.config import AUTH0_AUDIENCE, AUTH0_ISSUER, AUTH0_JWKS_URL
from app.db import DbSessionDep
from app.models import User

_jwks_client = PyJWKClient(AUTH0_JWKS_URL)

# Tolerated clock skew vs Auth0 for iat/nbf/exp (same default as Auth0's SDKs). Without it, a server
# clock even a few seconds slow rejects freshly issued tokens with "The token is not yet valid (iat)".
JWT_LEEWAY_S = 60
REFRESH_WAIT_INTERVAL_S = 0.1
CSRF_HEADER = "X-Requested-With"
_CSRF_PROTECTED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _session_expired() -> HTTPException:
    return HTTPException(status_code=401, detail="Session expired; please log in again")


def _decode_access_token(token: str) -> dict:
    signing_key = _jwks_client.get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=AUTH0_AUDIENCE,
        issuer=AUTH0_ISSUER,
        leeway=JWT_LEEWAY_S,
    )


def _wait_for_concurrent_refresh(session_id: str, expired_token: str) -> str:
    deadline = time.monotonic() + REFRESH_LOCK_TTL_SECONDS
    while time.monotonic() < deadline:
        time.sleep(REFRESH_WAIT_INTERVAL_S)
        # Check the lock before re-reading the session: if it's gone and the token is still the
        # expired one, the holder finished without refreshing.
        held = refresh_lock_held(session_id)
        current = get_session(session_id)
        if not current:
            raise _session_expired()
        if current.get("access_token") != expired_token:
            return current["access_token"]
        if not held:
            break
    raise _session_expired()


def _refresh_access_token(session_id: str, expired_token: str) -> str:
    # Auth0 refresh-token rotation revokes the whole token family if a used refresh token is
    # replayed, so only one request per session may refresh; the rest wait for its result.
    lock = acquire_refresh_lock(session_id)
    if lock is None:
        return _wait_for_concurrent_refresh(session_id, expired_token)

    try:
        session = get_session(session_id)
        if not session:
            raise _session_expired()
        if session.get("access_token") != expired_token:
            return session["access_token"]

        refresh_token = session.get("refresh_token")
        if not refresh_token:
            raise _session_expired()

        try:
            tokens = refresh_tokens(refresh_token)
        except httpx.HTTPStatusError as exc:
            raise _session_expired() from exc
        except httpx.TransportError as exc:
            raise HTTPException(status_code=503, detail="Auth provider unavailable; try again") from exc

        session = apply_refreshed_tokens(session, tokens)
        update_session(session_id, session)
        return session["access_token"]
    finally:
        release_refresh_lock(session_id, lock)


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

    access_token = _refresh_access_token(session_id, session["access_token"])

    try:
        return _decode_access_token(access_token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid refreshed token: {exc}") from exc


CurrentUserDep = Annotated[dict, Depends(get_current_user)]


def get_db_user(user: CurrentUserDep, db: DbSessionDep) -> User:
    sub = user.get("sub")
    db_user = db.query(User).filter(User.auth0_sub == sub).one_or_none() if sub else None
    if db_user is None:
        raise HTTPException(status_code=401, detail="User not registered; please log in again")
    return db_user


DbUserDep = Annotated[User, Depends(get_db_user)]


def require_csrf_header(request: Request) -> None:
    # Cross-site forms can't set custom headers, and cross-origin fetch needs a CORS preflight we never grant.
    if request.method in _CSRF_PROTECTED_METHODS and not request.headers.get(CSRF_HEADER):
        raise HTTPException(status_code=403, detail=f"Missing {CSRF_HEADER} header")


def require_permission(permission: str):
    def _check(user: CurrentUserDep) -> dict:
        if permission not in user.get("permissions", []):
            raise HTTPException(status_code=403, detail=f"Missing permission: {permission}")
        return user

    return _check
