import httpx

from app.config import AUTH0_CLIENT_ID, AUTH0_CLIENT_SECRET, AUTH0_DOMAIN


def refresh_tokens(refresh_token: str) -> dict:
    response = httpx.post(
        f"https://{AUTH0_DOMAIN}/oauth/token",
        json={
            "grant_type": "refresh_token",
            "client_id": AUTH0_CLIENT_ID,
            "client_secret": AUTH0_CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
    )
    response.raise_for_status()
    return response.json()
