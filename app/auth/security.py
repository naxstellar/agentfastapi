import httpx
import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

from app.auth.session_store import get_session, update_session
from app.auth.token_refresh import refresh_tokens
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

    session["access_token"] = tokens.get("access_token", session["access_token"])
    session["id_token"] = tokens.get("id_token", session.get("id_token"))
    session["refresh_token"] = tokens.get("refresh_token", refresh_token)
    update_session(session_id, session)

    try:
        return _decode_access_token(session["access_token"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid refreshed token: {exc}") from exc
