"""WP7 delete cascade against the real GCS bucket and a real Agent Engine sandbox.

Opt-in: `pytest -m external` (needs GCS_BUCKET, AGENT_ENGINE_NAME and ADC, see CLAUDE.md).
"""

import uuid

import httpx
import pytest

import app.config
from app.agent.sandbox import AgentEngineProvider, SandboxGone, SandboxManager, SandboxSlots
from app.agent.workspace import ConversationWorkspace
from app.main import app as fastapi_app
from app.models_agent import Artifact, Conversation, Run
from app.routers import conversations as conversations_module
from app.routers import datasets as datasets_router
from app.services.storage import get_storage

pytestmark = [
    pytest.mark.external,
    pytest.mark.skipif(not app.config.AGENT_ENGINE_NAME, reason="AGENT_ENGINE_NAME is not set in env/.env"),
]


async def test_delete_removes_real_artifact_object_and_real_sandbox(
    client, db, fake_async_redis, make_user, login_as, monkeypatch
):
    try:
        storage = get_storage()
    except Exception as exc:
        pytest.skip(f"GCS not configured: {exc}")
    fastapi_app.dependency_overrides[datasets_router._storage] = lambda: storage
    user = make_user(f"auth0|external-{uuid.uuid4().hex[:8]}")
    login_as(user)
    client.headers["X-Requested-With"] = "fetch"

    provider = AgentEngineProvider(app.config.AGENT_ENGINE_NAME, app.config.SANDBOX_MACHINE_CONFIG)
    conversation = Conversation(user_id=user.id, model="anthropic:claude-sonnet-5-5")
    db.add(conversation)
    db.flush()
    run = Run(conversation_id=conversation.id, user_id=user.id, status="running")
    db.add(run)
    db.commit()

    class _Session:
        def __enter__(self):
            return db

        def __exit__(self, *exc):
            return False

    workspace = ConversationWorkspace(
        user_id=user.id,
        conversation_id=conversation.id,
        run_id=run.id,
        manager=SandboxManager(provider, SandboxSlots(fake_async_redis, 5)),
        storage=storage,
        session_factory=_Session,
    )
    sandbox_name = None
    artifact_key = None
    try:
        result = await workspace.run_python("open('outputs/report.txt', 'w').write('hello wp7')\nprint('ok')")
        assert not result.is_error, result.output
        assert [a.filename for a in result.artifacts] == ["report.txt"]
        db.refresh(conversation)
        sandbox_name = conversation.sandbox_id
        assert sandbox_name
        artifact = db.query(Artifact).filter_by(run_id=run.id).one()
        artifact_key = artifact.gcs_key
        assert storage.object_size(artifact_key) is not None

        # The signed URL from the API serves the real object as an attachment.
        link = (await client.get(f"/api/v1/artifacts/{artifact.id}")).json()
        async with httpx.AsyncClient() as gcs:
            fetched = await gcs.get(link["url"])
        assert fetched.content == b"hello wp7"
        assert "attachment" in fetched.headers["content-disposition"]

        # The run is over; delete uses the real sandbox helper (sync SDK) with the test's Redis.
        run.status = "succeeded"
        db.commit()
        monkeypatch.setattr(conversations_module, "get_storage", lambda: storage)
        assert (await client.delete(f"/api/v1/conversations/{conversation.id}")).status_code == 204

        assert storage.object_size(artifact_key) is None
        with pytest.raises(SandboxGone):
            await provider.execute(sandbox_name, "print(1)", [], 30)
        db.expire_all()
        assert db.get(Conversation, conversation.id) is None
    finally:
        fastapi_app.dependency_overrides.pop(datasets_router._storage, None)
        if artifact_key:
            storage.delete(artifact_key)
        if sandbox_name:
            await provider.delete(sandbox_name)  # idempotent
