import pytest
from fastapi import APIRouter, FastAPI

from app.auth.security import get_current_user
from app.routers import api_v1
from tests.conftest import make_client

STATE_CHANGING = ["POST", "PUT", "PATCH", "DELETE"]


def _probe_app(claims: dict | None) -> FastAPI:
    # Same dependencies as the real /api/v1 router, plus state-changing routes to exercise them.
    router = APIRouter(prefix="/api/v1", dependencies=api_v1.router.dependencies)

    @router.api_route("/probe", methods=["GET", *STATE_CHANGING])
    def probe() -> dict:
        return {"ok": True}

    probe_app = FastAPI()
    probe_app.include_router(router)
    if claims is not None:
        probe_app.dependency_overrides[get_current_user] = lambda: claims
    return probe_app


@pytest.fixture
async def probe_client(claims):
    async with make_client(_probe_app(claims)) as c:
        yield c


@pytest.mark.parametrize("method", STATE_CHANGING)
async def test_state_changing_request_without_header_is_403(probe_client, method):
    response = await probe_client.request(method, "/api/v1/probe")
    assert response.status_code == 403


@pytest.mark.parametrize("method", STATE_CHANGING)
async def test_state_changing_request_with_header_passes(probe_client, method):
    response = await probe_client.request(method, "/api/v1/probe", headers={"X-Requested-With": "fetch"})
    assert response.status_code == 200


async def test_get_does_not_need_header(authed_client):
    response = await authed_client.get("/api/v1/me")
    assert response.status_code == 200


async def test_unauthenticated_post_without_header_is_rejected_by_csrf(redis_db):
    async with make_client(_probe_app(None)) as c:
        response = await c.post("/api/v1/probe")
    assert response.status_code == 403
