"""WP7: conversations CRUD, display history, delete cascade and artifact download URLs."""

import uuid

import pytest
from sqlalchemy import text

import app.routers.conversations as conversations_module
from app.agent.sandbox import SandboxUnavailableError
from app.models_agent import Artifact, Conversation, ConversationDataset, Dataset, Message, Run
from app.main import app as fastapi_app
from app.routers import datasets as datasets_router
from app.workers.tasks import run_agent
from tests.fakes import FakeStorage

CSRF = {"X-Requested-With": "fetch"}
MODEL = "anthropic:claude-sonnet-5-5"
API = "/api/v1"


@pytest.fixture
def alice(make_user, login_as):
    user = make_user("auth0|alice-conv")
    login_as(user)
    return user


@pytest.fixture
def storage():
    fake = FakeStorage()
    fastapi_app.dependency_overrides[datasets_router._storage] = lambda: fake
    monkey = conversations_module
    original = monkey.get_storage
    monkey.get_storage = lambda: fake
    yield fake
    monkey.get_storage = original


@pytest.fixture
def api(client, db):
    return client


async def create(api, **body) -> dict:
    response = await api.post(f"{API}/conversations", json=body, headers=CSRF)
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_uses_default_model_and_validates_override(api, alice):
    assert (await create(api))["model"]
    other = {"model": "not-a-model"}
    assert (await api.post(f"{API}/conversations", json=other, headers=CSRF)).status_code == 422
    created = await create(api, title="Sales")
    assert created["title"] == "Sales"


async def test_user_default_model_setting_is_used(api, alice, monkeypatch):
    monkeypatch.setattr(conversations_module, "ALLOWED_MODELS", ("a:one", "b:two"))
    monkeypatch.setattr("app.agent.models.ALLOWED_MODELS", ("a:one", "b:two"))
    monkeypatch.setattr("app.agent.models.DEFAULT_MODEL", "a:one")
    monkeypatch.setattr("app.routers.settings.ALLOWED_MODELS", ("a:one", "b:two"))
    await api.put(f"{API}/settings", json={"require_code_approval": False, "default_model": "b:two"}, headers=CSRF)
    assert (await create(api))["model"] == "b:two"


async def test_isolation_between_users(api, alice, make_user, login_as, db):
    mine = await create(api)
    bob = make_user("auth0|bob-conv")
    login_as(bob)
    cid = mine["id"]
    assert (await api.get(f"{API}/conversations/{cid}")).status_code == 404
    assert (await api.patch(f"{API}/conversations/{cid}", json={"title": "x"}, headers=CSRF)).status_code == 404
    assert (await api.delete(f"{API}/conversations/{cid}", headers=CSRF)).status_code == 404
    assert (await api.get(f"{API}/conversations/{cid}/messages")).status_code == 404
    assert (await api.get(f"{API}/conversations")).json()["items"] == []
    assert db.get(Conversation, uuid.UUID(cid)) is not None


async def test_rename_and_model_change(api, alice, db):
    conv = await create(api)
    renamed = await api.patch(f"{API}/conversations/{conv['id']}", json={"title": "Q3 revenue"}, headers=CSRF)
    assert renamed.json()["title"] == "Q3 revenue"
    assert (await api.patch(f"{API}/conversations/{conv['id']}", json={"title": ""}, headers=CSRF)).status_code == 422
    bad = await api.patch(f"{API}/conversations/{conv['id']}", json={"model": "nope"}, headers=CSRF)
    assert bad.status_code == 422

    db.add(Run(conversation_id=uuid.UUID(conv["id"]), user_id=alice.id, status="running"))
    db.flush()
    busy = await api.patch(f"{API}/conversations/{conv['id']}", json={"model": "openai:other"}, headers=CSRF)
    assert busy.status_code == 409


async def test_pagination_is_stable_while_conversations_are_added_and_renamed(api, alice):
    ids = [(await create(api, title=f"c{i}"))["id"] for i in range(5)]  # oldest first
    first = (await api.get(f"{API}/conversations?limit=2")).json()
    assert [c["id"] for c in first["items"]] == ids[:2:-1] or [c["id"] for c in first["items"]] == [ids[4], ids[3]]
    await create(api, title="newer")  # arrives between pages
    await api.patch(f"{API}/conversations/{ids[0]}", json={"title": "renamed"}, headers=CSRF)
    second = (await api.get(f"{API}/conversations?limit=2&cursor={first['next_cursor']}")).json()
    assert [c["id"] for c in second["items"]] == [ids[2], ids[1]]
    third = (await api.get(f"{API}/conversations?limit=2&cursor={second['next_cursor']}")).json()
    assert [c["id"] for c in third["items"]] == [ids[0]]
    assert third["next_cursor"] is None
    assert (await api.get(f"{API}/conversations?cursor=garbage")).status_code == 400


async def test_first_message_sets_the_title_once(api, alice, monkeypatch):
    monkeypatch.setattr(run_agent, "apply_async", lambda **kwargs: None)
    conv = await create(api)
    long_text = "Please summarise the quarterly revenue by region " * 5
    assert (await api.post(f"{API}/conversations/{conv['id']}/runs", json={"text": long_text}, headers=CSRF)).status_code == 202
    title = (await api.get(f"{API}/conversations/{conv['id']}")).json()["title"]
    assert title.startswith("Please summarise the quarterly revenue by region")
    assert len(title) <= conversations_module.AUTO_TITLE_CHARS and title.endswith("…")

    named = await create(api, title="Mine")
    await api.post(f"{API}/conversations/{named['id']}/runs", json={"text": "hello"}, headers=CSRF)
    assert (await api.get(f"{API}/conversations/{named['id']}")).json()["title"] == "Mine"


async def test_messages_page_newest_first_returned_oldest_first(api, alice, db):
    conv = await create(api)
    cid = uuid.UUID(conv["id"])
    for i in range(5):
        db.add(Message(conversation_id=cid, role="user", content={"text": f"m{i}"}))
    db.flush()
    latest = (await api.get(f"{API}/conversations/{conv['id']}/messages?limit=2")).json()
    assert [m["content"]["text"] for m in latest["items"]] == ["m3", "m4"]
    db.add(Message(conversation_id=cid, role="user", content={"text": "m5"}))
    db.flush()
    older = (await api.get(f"{API}/conversations/{conv['id']}/messages?limit=2&before={latest['next_before']}")).json()
    assert [m["content"]["text"] for m in older["items"]] == ["m1", "m2"]
    oldest = (await api.get(f"{API}/conversations/{conv['id']}/messages?limit=2&before={older['next_before']}")).json()
    assert [m["content"]["text"] for m in oldest["items"]] == ["m0"]
    assert oldest["next_before"] is None


def make_checkpoint_tables(db):
    for table in conversations_module.CHECKPOINT_TABLES:
        db.execute(text(f"CREATE TABLE {table} (thread_id text NOT NULL, n int)"))


async def test_delete_removes_everything_but_datasets(api, alice, db, storage, monkeypatch):
    deleted = []
    monkeypatch.setattr(conversations_module, "delete_sandbox_sync", lambda name, client: deleted.append(name))
    make_checkpoint_tables(db)
    conv = await create(api)
    cid = uuid.UUID(conv["id"])
    keep = await create(api)
    conversation = db.get(Conversation, cid)
    conversation.sandbox_id = "sandboxes/abc"
    dataset = Dataset(user_id=alice.id, filename="a.csv", gcs_key="users/x/a.csv", size_bytes=1, content_type="text/csv", status="ready")
    db.add(dataset)
    run = Run(conversation_id=cid, user_id=alice.id, status="succeeded")
    db.add(run)
    db.flush()
    db.add(ConversationDataset(conversation_id=cid, dataset_id=dataset.id))
    db.add(Message(conversation_id=cid, role="user", content={"text": "hi"}))
    db.add(Artifact(run_id=run.id, user_id=alice.id, kind="image", gcs_key="users/x/artifacts/chart.png", filename="chart.png"))
    storage.objects["users/x/artifacts/chart.png"] = (b"png", "image/png")
    for thread in (conv["id"], keep["id"]):
        db.execute(text("INSERT INTO checkpoints VALUES (:t, 1)"), {"t": thread})
        db.execute(text("INSERT INTO checkpoint_blobs VALUES (:t, 1)"), {"t": thread})
        db.execute(text("INSERT INTO checkpoint_writes VALUES (:t, 1)"), {"t": thread})
    db.flush()

    assert (await api.delete(f"{API}/conversations/{conv['id']}", headers=CSRF)).status_code == 204

    assert deleted == ["sandboxes/abc"]
    assert "users/x/artifacts/chart.png" not in storage.objects
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        assert db.execute(text(f"SELECT thread_id FROM {table}")).scalars().all() == [keep["id"]]
    db.expire_all()
    assert db.get(Conversation, cid) is None
    assert db.get(Conversation, uuid.UUID(keep["id"])) is not None
    assert db.get(Dataset, dataset.id) is not None
    assert db.scalar(text("SELECT count(*) FROM messages WHERE conversation_id = :c"), {"c": cid}) == 0
    assert db.scalar(text("SELECT count(*) FROM runs WHERE conversation_id = :c"), {"c": cid}) == 0
    assert db.scalar(text("SELECT count(*) FROM conversation_datasets")) == 0
    assert db.scalar(text("SELECT count(*) FROM artifacts")) == 0


async def test_delete_without_checkpoint_tables_still_works(api, alice, db):
    conv = await create(api)
    assert (await api.delete(f"{API}/conversations/{conv['id']}", headers=CSRF)).status_code == 204
    assert (await api.get(f"{API}/conversations/{conv['id']}")).status_code == 404


async def test_delete_refused_while_a_run_is_active(api, alice, db):
    conv = await create(api)
    for status in ("queued", "running", "awaiting_approval"):
        run = Run(conversation_id=uuid.UUID(conv["id"]), user_id=alice.id, status=status)
        db.add(run)
        db.flush()
        assert (await api.delete(f"{API}/conversations/{conv['id']}", headers=CSRF)).status_code == 409
        db.delete(run)
        db.flush()
    assert (await api.delete(f"{API}/conversations/{conv['id']}", headers=CSRF)).status_code == 204


async def test_delete_keeps_the_row_when_the_sandbox_cannot_be_deleted(api, alice, db, monkeypatch):
    def broken(name, client):
        raise SandboxUnavailableError("down")

    monkeypatch.setattr(conversations_module, "delete_sandbox_sync", broken)
    conv = await create(api)
    db.get(Conversation, uuid.UUID(conv["id"])).sandbox_id = "sandboxes/abc"
    db.flush()
    assert (await api.delete(f"{API}/conversations/{conv['id']}", headers=CSRF)).status_code == 503
    assert (await api.get(f"{API}/conversations/{conv['id']}")).status_code == 200


async def test_artifact_download_url_is_owner_only(api, alice, db, storage, make_user, login_as):
    conv = await create(api)
    run = Run(conversation_id=uuid.UUID(conv["id"]), user_id=alice.id, status="succeeded")
    db.add(run)
    db.flush()
    artifact = Artifact(run_id=run.id, user_id=alice.id, kind="image", gcs_key="users/x/artifacts/c.png", filename="c.png")
    db.add(artifact)
    db.flush()

    ok = await api.get(f"{API}/artifacts/{artifact.id}")
    assert ok.status_code == 200 and ok.json()["method"] == "GET"
    login_as(make_user("auth0|bob-art"))
    assert (await api.get(f"{API}/artifacts/{artifact.id}")).status_code == 404


async def test_attached_datasets_listing_is_per_conversation_and_owner_only(api, alice, db, make_user, login_as):
    conv, other = await create(api), await create(api)
    dataset = Dataset(user_id=alice.id, filename="a.csv", gcs_key="users/x/a.csv", size_bytes=1, content_type="text/csv", status="ready")
    db.add(dataset)
    db.flush()
    db.add(ConversationDataset(conversation_id=uuid.UUID(conv["id"]), dataset_id=dataset.id))
    db.flush()
    attached = (await api.get(f"{API}/conversations/{conv['id']}/datasets")).json()
    assert [d["filename"] for d in attached] == ["a.csv"]
    assert (await api.get(f"{API}/conversations/{other['id']}/datasets")).json() == []
    login_as(make_user("auth0|bob-ds"))
    assert (await api.get(f"{API}/conversations/{conv['id']}/datasets")).status_code == 404


async def test_active_run_endpoint(api, alice, db, make_user, login_as):
    conv = await create(api)
    cid = uuid.UUID(conv["id"])
    assert (await api.get(f"{API}/conversations/{conv['id']}/active-run")).json() is None
    db.add(Run(conversation_id=cid, user_id=alice.id, status="succeeded"))
    paused = Run(conversation_id=cid, user_id=alice.id, status="awaiting_approval")
    db.add(paused)
    db.flush()
    body = (await api.get(f"{API}/conversations/{conv['id']}/active-run")).json()
    assert body["id"] == str(paused.id) and body["status"] == "awaiting_approval"
    login_as(make_user("auth0|bob-run"))
    assert (await api.get(f"{API}/conversations/{conv['id']}/active-run")).status_code == 404


async def test_settings_lists_allowed_models(api, alice):
    body = (await api.get(f"{API}/settings")).json()
    assert body["allowed_models"] and body["default_model"] is None
    saved = (await api.put(f"{API}/settings", json={"require_code_approval": True}, headers=CSRF)).json()
    assert saved["allowed_models"] == body["allowed_models"]


async def test_static_modules_are_served_as_javascript(api):
    for path in ("/static/js/app.js", "/static/vendor/marked.min.js"):
        response = await api.get(path)
        assert response.status_code == 200 and "javascript" in response.headers["content-type"]
