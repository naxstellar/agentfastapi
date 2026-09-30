from fastapi import APIRouter, FastAPI

from app.auth.security import DbUserDep, get_current_user
from app.db import get_db
from app.models import User
from tests.conftest import make_client


def _probe_app(db, claims: dict) -> FastAPI:
    router = APIRouter()

    @router.get("/whoami")
    def whoami(user: DbUserDep) -> dict:
        return {"id": str(user.id), "sub": user.auth0_sub}

    probe = FastAPI()
    probe.include_router(router)
    probe.dependency_overrides[get_current_user] = lambda: claims
    probe.dependency_overrides[get_db] = lambda: db
    return probe


async def test_db_user_resolves_the_matching_user(db):
    # Several users inserted in order; each must resolve to itself, not to whichever row comes first.
    users = [User(auth0_sub=f"auth0|user-{i}") for i in range(3)]
    db.add_all(users)
    db.flush()

    for expected in reversed(users):
        async with make_client(_probe_app(db, {"sub": expected.auth0_sub})) as c:
            response = await c.get("/whoami")

        assert response.status_code == 200
        assert response.json() == {"id": str(expected.id), "sub": expected.auth0_sub}


async def test_db_user_401_when_no_users_row(db):
    async with make_client(_probe_app(db, {"sub": "auth0|never-logged-in"})) as c:
        response = await c.get("/whoami")

    assert response.status_code == 401


async def test_db_user_401_when_token_has_no_sub(db, user):
    async with make_client(_probe_app(db, {})) as c:
        response = await c.get("/whoami")

    assert response.status_code == 401
