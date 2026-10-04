"""WP8A: media the model returns becomes a downloadable artifact."""

import base64
import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import select

from app.agent.media import extract_media
from app.models_agent import Artifact, Message
from app.workers.runner import execute_run
from tests.test_run_worker import (  # noqa: F401  (fixtures)
    assistant_messages,
    make_deps,
    new_run,
    persistence,
    stream_events,
    types,
)

PNG = bytes.fromhex("89504e470d0a1a0a") + b"0" * 32
RUN = uuid.uuid4()


def block(data: bytes, type_="image", **extra) -> dict:
    return {"type": type_, "base64": base64.b64encode(data).decode(), "mime_type": "image/png", **extra}


def test_extract_saves_known_media_and_names_it():
    found, notes = extract_media(RUN, AIMessage([{"type": "text", "text": "hi"}, block(PNG)]), 1000)
    assert notes == [] and len(found) == 1
    assert found[0].content_type == "image/png" and found[0].filename == "media-1.png"


def test_extract_ignores_the_declared_type_and_checks_the_bytes():
    found, notes = extract_media(RUN, AIMessage([block(b"<svg onload=alert(1)>")]), 1000)
    assert found == [] and "unsupported" in notes[0]


def test_extract_skips_url_only_oversize_and_duplicate_blocks():
    message = AIMessage([{"type": "image", "url": "https://example.com/a.png"}, block(PNG), block(PNG), block(PNG + b"1" * 2000)])
    found, notes = extract_media(RUN, message, 1000)
    assert len(found) == 1 and len(notes) == 2


def test_extract_reads_tool_results_too_and_plain_strings_are_ignored():
    assert extract_media(RUN, ToolMessage([block(PNG)], tool_call_id="c"), 1000)[0]
    assert extract_media(RUN, AIMessage("just text"), 1000) == ([], [])


def test_same_bytes_get_the_same_id_per_run_only():
    one = extract_media(RUN, AIMessage([block(PNG)]), 1000)[0][0]
    again = extract_media(RUN, AIMessage([block(PNG)]), 1000)[0][0]
    other = extract_media(uuid.uuid4(), AIMessage([block(PNG)]), 1000)[0][0]
    assert one.artifact_id == again.artifact_id != other.artifact_id


def image_then_answer(messages):
    if isinstance(messages[-1], HumanMessage):
        return AIMessage([{"type": "text", "text": "Here is your picture."}, block(PNG)])
    return AIMessage("?")


async def test_run_saves_model_media_as_a_downloadable_artifact(db, make_deps, new_run, fake_async_redis):
    run = new_run("draw something")
    deps = make_deps(image_then_answer)
    await execute_run(deps, run.id)

    rows = db.scalars(select(Artifact).where(Artifact.run_id == run.id)).all()
    assert [(r.kind, r.filename) for r in rows] == [("image", "media-1.png")]
    assert deps.storage.objects[rows[0].gcs_key] == PNG
    evts = await stream_events(fake_async_redis, run.id)
    artifact_events = [d for t, d in evts if t == "artifact"]
    assert artifact_events == [{"artifact_id": str(rows[0].id), "kind": "image", "filename": "media-1.png"}]
    reply = assistant_messages(db, run)[0].content
    assert reply["text"] == "Here is your picture."
    assert reply["artifacts"] == [{"id": str(rows[0].id), "kind": "image", "filename": "media-1.png"}]
    assert "base64" not in str(reply)
