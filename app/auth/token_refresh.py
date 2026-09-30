import httpx

from app.config import AUTH0_CLIENT_ID, AUTH0_CLIENT_SECRET, AUTH0_DOMAIN

# Per phase (connect/read/...); keeps a refresh well inside the 10 s per-session refresh lock TTL.
REFRESH_TIMEOUT = httpx.Timeout(3.0)


def refresh_tokens(refresh_token: str) -> dict:
    response = httpx.post(
        f"https://{AUTH0_DOMAIN}/oauth/token",
        json={
            "grant_type": "refresh_token",
            "client_id": AUTH0_CLIENT_ID,
            "client_secret": AUTH0_CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
        timeout=REFRESH_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def apply_refreshed_tokens(session: dict, tokens: dict) -> dict:
    session["access_token"] = tokens.get("access_token", session.get("access_token"))
    session["id_token"] = tokens.get("id_token", session.get("id_token"))
    session["refresh_token"] = tokens.get("refresh_token", session.get("refresh_token"))
    return session
