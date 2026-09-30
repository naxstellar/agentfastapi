import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import security
from app.auth.session_store import create_session
from app.config import AUTH0_AUDIENCE, AUTH0_ISSUER
from tests.conftest import session_cookie

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def local_jwks(monkeypatch):
    fake_client = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=_KEY.public_key()))
    monkeypatch.setattr(security, "_jwks_client", fake_client)


def _mint(**overrides) -> str:
    now = int(time.time())
    claims = {"sub": "auth0|test-user", "aud": AUTH0_AUDIENCE, "iss": AUTH0_ISSUER, "iat": now, "exp": now + 3600}
    return jwt.encode(claims | overrides, _KEY, algorithm="RS256")


def test_token_issued_by_a_slightly_faster_clock_is_accepted():
    # Auth0's clock a few seconds ahead of ours: iat is in our future.
    token = _mint(iat=int(time.time()) + 5)
    assert security._decode_access_token(token)["sub"] == "auth0|test-user"


def test_iat_beyond_leeway_is_rejected():
    token = _mint(iat=int(time.time()) + security.JWT_LEEWAY_S + 60)
    with pytest.raises(jwt.ImmatureSignatureError):
        security._decode_access_token(token)


def test_expiry_beyond_leeway_still_raises_expired_so_refresh_triggers():
    now = int(time.time())
    token = _mint(iat=now - 7200, exp=now - security.JWT_LEEWAY_S - 60)
    with pytest.raises(jwt.ExpiredSignatureError):
        security._decode_access_token(token)


async def test_me_right_after_login_with_skewed_clock_is_200(client):
    session_id = create_session({"access_token": _mint(iat=int(time.time()) + 5), "claims": {}})

    response = await client.get("/api/v1/me", headers=session_cookie(session_id))

    assert response.status_code == 200
    assert response.json()["sub"] == "auth0|test-user"
