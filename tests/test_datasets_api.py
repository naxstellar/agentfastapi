import io
import uuid
from urllib.parse import urlparse

import openpyxl
import pytest
from sqlalchemy import select

from app.config import MAX_UPLOAD_BYTES
from app.main import app as fastapi_app
from app.models_agent import Conversation, ConversationDataset, Dataset
from app.routers import datasets as datasets_router
from app.services.dataset_inspect import CONTENT_TYPES
from app.workers.datasets import run_process_dataset
from tests.fakes import FakeStorage

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        ws = workbook.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    workbook.save(buf)
    return buf.getvalue()


SALES_XLSX = xlsx_bytes({"Sales": [["region", "amount"], ["north", 10], ["south", 20]], "Notes": [["text"]]})


@pytest.fixture
def storage() -> FakeStorage:
    fake = FakeStorage()
    fastapi_app.dependency_overrides[datasets_router._storage] = lambda: fake
    return fake  # overrides are cleared by the `client` fixture


@pytest.fixture
def enqueued(monkeypatch) -> list[uuid.UUID]:
    calls: list[uuid.UUID] = []
    monkeypatch.setattr(datasets_router, "enqueue_process_dataset", calls.append)
    return calls


@pytest.fixture
async def api(client, db, storage, enqueued):
    client.headers["X-Requested-With"] = "fetch"
    return client


@pytest.fixture
def alice(make_user, login_as):
    user = make_user("auth0|alice")
    login_as(user)
    return user


@pytest.fixture
def bob(make_user):
    return make_user("auth0|bob")


def _key_from_url(url: str) -> str:
    return urlparse(url).path.lstrip("/")


async def create(api, filename: str, size: int):
    return await api.post("/api/v1/datasets", json={"filename": filename, "size_bytes": size})


async def upload(api, storage: FakeStorage, filename: str, data: bytes) -> dict:
    response = await create(api, filename, len(data))
    assert response.status_code == 201, response.text
    body = response.json()
    put_status = storage.put(_key_from_url(body["upload"]["url"]), data, body["upload"]["headers"]["Content-Type"])
    assert put_status == 200
    return body["dataset"]


async def upload_and_process(api, storage: FakeStorage, db, filename: str, data: bytes) -> dict:
    dataset = await upload(api, storage, filename, data)
    response = await api.post(f"/api/v1/datasets/{dataset['id']}/complete")
    assert response.status_code == 202, response.text
    run_process_dataset(db, storage, uuid.UUID(dataset["id"]))
    return (await api.get(f"/api/v1/datasets/{dataset['id']}")).json()


async def test_create_returns_upload_url_bound_to_type_and_declared_size(api, storage, alice, db):
    response = await create(api, "Sales Q1.xlsx", len(SALES_XLSX))

    assert response.status_code == 201
    body = response.json()
    dataset_id = body["dataset"]["id"]
    assert body["dataset"]["status"] == "pending"
    assert body["upload"]["method"] == "PUT"
    assert body["upload"]["headers"] == {
        "Content-Type": CONTENT_TYPES[".xlsx"],
        "x-goog-content-length-range": f"0,{len(SALES_XLSX)}",
    }
    key = _key_from_url(body["upload"]["url"])
    assert key == f"pending/users/{alice.id}/datasets/{dataset_id}/Sales_Q1.xlsx"
    assert db.get(Dataset, uuid.UUID(dataset_id)).gcs_key == key

    # What GCS does with the signed headers: bigger bodies and other content types are refused.
    assert storage.put(key, SALES_XLSX + b"x", CONTENT_TYPES[".xlsx"]) == 400
    assert storage.put(key, SALES_XLSX, "text/html") == 403


@pytest.mark.parametrize("filename", ["report.pdf", "script.exe", "no_extension", "archive.xlsx.zip"])
async def test_create_rejects_unsupported_types(api, alice, filename):
    response = await create(api, filename, 100)
    assert response.status_code == 415


async def test_create_rejects_files_over_upload_limit(api, alice):
    response = await create(api, "big.csv", MAX_UPLOAD_BYTES + 1)
    assert response.status_code == 413


async def test_create_rejects_when_storage_quota_exceeded(api, alice, db, monkeypatch):
    monkeypatch.setattr(datasets_router, "MAX_USER_STORAGE_BYTES", 100)

    first = await create(api, "a.csv", 60)
    assert first.status_code == 201
    assert (await create(api, "b.csv", 60)).status_code == 413

    # Rejected datasets don't count toward the quota.
    db.get(Dataset, uuid.UUID(first.json()["dataset"]["id"])).status = "rejected"
    db.flush()
    assert (await create(api, "c.csv", 60)).status_code == 201


async def test_path_components_in_filename_are_dropped(api, alice):
    response = await create(api, "..\\..//etc/évil name.csv", 10)

    assert response.status_code == 201
    body = response.json()
    assert body["dataset"]["filename"] == "évil name.csv"
    key = _key_from_url(body["upload"]["url"])
    assert key.endswith("/evil_name.csv")
    assert ".." not in key


async def test_complete_without_upload_is_409(api, alice):
    dataset = (await create(api, "a.csv", 10)).json()["dataset"]

    response = await api.post(f"/api/v1/datasets/{dataset['id']}/complete")

    assert response.status_code == 409


async def test_complete_moves_object_out_of_pending_and_enqueues(api, storage, enqueued, alice, db):
    dataset = await upload(api, storage, "data.csv", b"a,b\n1,2\n")

    response = await api.post(f"/api/v1/datasets/{dataset['id']}/complete")

    assert response.status_code == 202
    final_key = f"users/{alice.id}/datasets/{dataset['id']}/data.csv"
    assert list(storage.objects) == [final_key]
    assert db.get(Dataset, uuid.UUID(dataset["id"])).gcs_key == final_key
    assert enqueued == [uuid.UUID(dataset["id"])]

    # Retrying while still pending is safe and re-enqueues (recovers from a failed enqueue).
    again = await api.post(f"/api/v1/datasets/{dataset['id']}/complete")
    assert again.status_code == 202
    assert list(storage.objects) == [final_key]
    assert len(enqueued) == 2


async def test_complete_recovers_when_object_was_moved_but_row_not_updated(api, storage, alice):
    dataset = await upload(api, storage, "data.csv", b"a,b\n1,2\n")
    final_key = f"users/{alice.id}/datasets/{dataset['id']}/data.csv"
    storage.move(f"pending/{final_key}", final_key)

    response = await api.post(f"/api/v1/datasets/{dataset['id']}/complete")

    assert response.status_code == 202
    assert list(storage.objects) == [final_key]


async def test_complete_records_actual_uploaded_size(api, storage, alice):
    response = await create(api, "data.csv", 1000)
    body = response.json()
    storage.put(_key_from_url(body["upload"]["url"]), b"a,b\n1,2\n", body["upload"]["headers"]["Content-Type"])

    completed = await api.post(f"/api/v1/datasets/{body['dataset']['id']}/complete")

    assert completed.json()["size_bytes"] == len(b"a,b\n1,2\n")


async def test_valid_xlsx_becomes_ready_with_sheet_metadata(api, storage, db, alice):
    dataset = await upload_and_process(api, storage, db, "sales.xlsx", SALES_XLSX)

    assert dataset["status"] == "ready"
    assert dataset["reject_reason"] is None
    assert dataset["sheets"] == [
        {"name": "Sales", "columns": ["region", "amount"], "row_count": 2, "rows_truncated": False},
        {"name": "Notes", "columns": ["text"], "row_count": 0, "rows_truncated": False},
    ]


async def test_renamed_binary_is_rejected_and_its_object_deleted(api, storage, db, alice):
    dataset = await upload_and_process(api, storage, db, "photo.xlsx", PNG_BYTES)

    assert dataset["status"] == "rejected"
    assert "not an .xlsx" in dataset["reject_reason"]
    assert storage.objects == {}


async def test_binary_renamed_to_csv_is_rejected(api, storage, db, alice):
    dataset = await upload_and_process(api, storage, db, "photo.csv", PNG_BYTES)

    assert dataset["status"] == "rejected"
    assert storage.objects == {}


async def test_delete_removes_object_and_row(api, storage, db, alice):
    dataset = await upload_and_process(api, storage, db, "sales.xlsx", SALES_XLSX)
    assert storage.objects

    response = await api.delete(f"/api/v1/datasets/{dataset['id']}")

    assert response.status_code == 204
    assert storage.objects == {}
    assert (await api.get(f"/api/v1/datasets/{dataset['id']}")).status_code == 404


async def test_delete_of_pending_upload_removes_pending_object(api, storage, alice):
    dataset = await upload(api, storage, "data.csv", b"a\n1\n")

    response = await api.delete(f"/api/v1/datasets/{dataset['id']}")

    assert response.status_code == 204
    assert storage.objects == {}


async def test_download_url_only_for_ready_datasets(api, storage, db, alice):
    pending = await upload(api, storage, "data.csv", b"a\n1\n")
    assert (await api.get(f"/api/v1/datasets/{pending['id']}/download")).status_code == 409

    ready = await upload_and_process(api, storage, db, "sales.xlsx", SALES_XLSX)
    response = await api.get(f"/api/v1/datasets/{ready['id']}/download")

    assert response.status_code == 200
    assert response.json()["method"] == "GET"
    assert f"users/{alice.id}/datasets/{ready['id']}/sales.xlsx" in response.json()["url"]


async def test_list_returns_only_own_datasets(api, storage, alice, bob, login_as):
    mine = await upload(api, storage, "mine.csv", b"a\n1\n")
    login_as(bob)
    await upload(api, storage, "bobs.csv", b"a\n1\n")
    login_as(alice)

    response = await api.get("/api/v1/datasets")

    assert [d["id"] for d in response.json()] == [mine["id"]]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/datasets/{id}"),
        ("GET", "/api/v1/datasets/{id}/download"),
        ("POST", "/api/v1/datasets/{id}/complete"),
        ("DELETE", "/api/v1/datasets/{id}"),
    ],
)
async def test_other_users_dataset_is_404(api, storage, db, alice, bob, login_as, method, path):
    dataset = await upload_and_process(api, storage, db, "sales.xlsx", SALES_XLSX)
    objects_before = dict(storage.objects)
    login_as(bob)

    response = await api.request(method, path.format(id=dataset["id"]))

    assert response.status_code == 404
    assert storage.objects == objects_before


async def _conversation(db, user) -> Conversation:
    conversation = Conversation(user_id=user.id, model="anthropic:claude-sonnet-5-5")
    db.add(conversation)
    db.flush()
    return conversation


def _links(db, conversation: Conversation) -> list[uuid.UUID]:
    return list(
        db.scalars(
            select(ConversationDataset.dataset_id).where(ConversationDataset.conversation_id == conversation.id)
        )
    )


async def test_attach_and_detach(api, storage, db, alice):
    conversation = await _conversation(db, alice)
    dataset = await upload_and_process(api, storage, db, "sales.xlsx", SALES_XLSX)
    url = f"/api/v1/conversations/{conversation.id}/datasets"

    assert (await api.post(url, json={"dataset_id": dataset["id"]})).status_code == 204
    assert (await api.post(url, json={"dataset_id": dataset["id"]})).status_code == 204  # idempotent
    assert _links(db, conversation) == [uuid.UUID(dataset["id"])]

    assert (await api.delete(f"{url}/{dataset['id']}")).status_code == 204
    assert _links(db, conversation) == []


async def test_attach_requires_ready_dataset(api, storage, db, alice):
    conversation = await _conversation(db, alice)
    pending = await upload(api, storage, "data.csv", b"a\n1\n")

    response = await api.post(f"/api/v1/conversations/{conversation.id}/datasets", json={"dataset_id": pending["id"]})

    assert response.status_code == 409


async def test_cannot_attach_another_users_dataset_or_to_another_users_conversation(
    api, storage, db, alice, bob, login_as
):
    alice_conversation = await _conversation(db, alice)
    alice_dataset = await upload_and_process(api, storage, db, "a.xlsx", SALES_XLSX)
    login_as(bob)
    bob_conversation = await _conversation(db, bob)
    bob_dataset = await upload_and_process(api, storage, db, "b.xlsx", SALES_XLSX)

    foreign_dataset = await api.post(
        f"/api/v1/conversations/{bob_conversation.id}/datasets", json={"dataset_id": alice_dataset["id"]}
    )
    foreign_conversation = await api.post(
        f"/api/v1/conversations/{alice_conversation.id}/datasets", json={"dataset_id": bob_dataset["id"]}
    )
    foreign_detach = await api.delete(f"/api/v1/conversations/{alice_conversation.id}/datasets/{bob_dataset['id']}")

    assert foreign_dataset.status_code == 404
    assert foreign_conversation.status_code == 404
    assert foreign_detach.status_code == 404
    assert _links(db, alice_conversation) == []
    assert _links(db, bob_conversation) == []


async def test_dataset_endpoints_require_csrf_header_for_writes(api, alice):
    del api.headers["X-Requested-With"]

    response = await create(api, "a.csv", 10)

    assert response.status_code == 403


async def test_delete_also_removes_object_moved_by_an_uncommitted_complete(api, storage, alice):
    dataset = await upload(api, storage, "data.csv", b"a\n1\n")
    final_key = f"users/{alice.id}/datasets/{dataset['id']}/data.csv"
    storage.move(f"pending/{final_key}", final_key)  # row still points at the pending key

    response = await api.delete(f"/api/v1/datasets/{dataset['id']}")

    assert response.status_code == 204
    assert storage.objects == {}


async def test_storage_outage_is_503_not_500(api, storage, alice, monkeypatch):
    from google.api_core.exceptions import ServiceUnavailable

    def unavailable(*args, **kwargs):
        raise ServiceUnavailable("gcs down for users/<id>/datasets/<id>/secret.csv")

    monkeypatch.setattr(storage, "upload_url", unavailable)

    response = await create(api, "a.csv", 10)

    assert response.status_code == 503
    assert "secret" not in response.text
