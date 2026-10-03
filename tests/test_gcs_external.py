"""End-to-end against the real dev bucket. Opt-in: `pytest -m external` (needs GCS_BUCKET + ADC, see CLAUDE.md)."""

import uuid
from urllib.parse import urlparse

import httpx
import pytest

from app.main import app as fastapi_app
from app.routers import datasets as datasets_router
from app.services.storage import get_storage
from app.workers.datasets import run_process_dataset
from tests.test_datasets_api import SALES_XLSX

pytestmark = pytest.mark.external


@pytest.fixture
def real_storage():
    try:
        storage = get_storage()
    except Exception as exc:  # not configured / no ADC
        pytest.skip(f"GCS not configured: {exc}")
    fastapi_app.dependency_overrides[datasets_router._storage] = lambda: storage
    return storage


@pytest.fixture
async def api(client, db, real_storage, monkeypatch, make_user, login_as):
    # Process inline instead of through Celery, using the test DB session.
    monkeypatch.setattr(
        datasets_router, "enqueue_process_dataset", lambda dataset_id: run_process_dataset(db, real_storage, dataset_id)
    )
    login_as(make_user(f"auth0|external-{uuid.uuid4().hex[:8]}"))
    client.headers["X-Requested-With"] = "fetch"
    return client


async def test_signed_url_upload_process_download_delete(api, real_storage):
    created = await api.post("/api/v1/datasets", json={"filename": "sales.xlsx", "size_bytes": len(SALES_XLSX)})
    assert created.status_code == 201, created.text
    dataset_id = created.json()["dataset"]["id"]
    upload = created.json()["upload"]
    pending_key = urlparse(upload["url"]).path.split("/", 2)[-1]
    try:
        async with httpx.AsyncClient() as gcs:
            # GCS itself enforces the signed size range and content type.
            too_big = await gcs.put(upload["url"], content=SALES_XLSX + b"x", headers=upload["headers"])
            wrong_type = await gcs.put(
                upload["url"], content=SALES_XLSX, headers=upload["headers"] | {"Content-Type": "text/html"}
            )
            ok = await gcs.put(upload["url"], content=SALES_XLSX, headers=upload["headers"])
        assert too_big.status_code == 400, too_big.text
        assert wrong_type.status_code == 403, wrong_type.text
        assert ok.status_code == 200, ok.text

        completed = await api.post(f"/api/v1/datasets/{dataset_id}/complete")
        assert completed.status_code == 202, completed.text
        dataset = (await api.get(f"/api/v1/datasets/{dataset_id}")).json()
        assert dataset["status"] == "ready", dataset
        assert dataset["sheets"][0]["columns"] == ["region", "amount"]
        assert real_storage.object_size(pending_key) is None

        download = (await api.get(f"/api/v1/datasets/{dataset_id}/download")).json()
        async with httpx.AsyncClient() as gcs:
            fetched = await gcs.get(download["url"])
        assert fetched.status_code == 200
        assert fetched.content == SALES_XLSX
        assert "attachment" in fetched.headers["content-disposition"]

        final_key = urlparse(download["url"]).path.split("/", 2)[-1]
        assert (await api.delete(f"/api/v1/datasets/{dataset_id}")).status_code == 204
        assert real_storage.object_size(final_key) is None
    finally:
        real_storage.delete(pending_key)
        real_storage.delete(pending_key.removeprefix("pending/"))
