import asyncio
import time
import uuid
from contextlib import contextmanager

import pytest
from google.api_core.exceptions import ServiceUnavailable
from sqlalchemy import select

from app.agent.sandbox import (
    LEASES_KEY,
    ExecResult,
    ExecTimeout,
    OutputFile,
    SandboxGone,
    SandboxManager,
    SandboxSlots,
    SandboxUnavailableError,
    datasets_key,
)
from app.agent.workspace import (
    MAX_ARTIFACTS_PER_EXEC,
    MAX_OUTPUT_CHARS,
    RESET_NOTE,
    ConversationGoneError,
    ConversationWorkspace,
    clean_stderr,
    housekeeping_code,
    is_artifact_path,
    sandbox_paths,
    truncate,
)
from app.models_agent import Artifact, Conversation, ConversationDataset, Dataset, Run
from tests.fakes import FakeSandboxProvider, FakeStorage

# --- pure helpers ---------------------------------------------------------------------------------------


def test_truncate_keeps_head_and_tail():
    text = "A" * 8000 + "B" * 8000
    out = truncate(text)
    assert out.startswith("A" * 100) and out.endswith("B" * 100)
    assert "[6000 characters truncated]" in out
    assert len(out) < MAX_OUTPUT_CHARS + 100
    assert truncate("short") == "short"


def test_sandbox_paths_disambiguate_colliding_names():
    a, b, c = (uuid.UUID(int=i) for i in (1, 2, 3))
    paths = sandbox_paths([(a, "sales.xlsx"), (b, "sales.xlsx"), (c, "../other file.csv")])
    assert paths[a] == f"data/sales_{a.hex[:8]}.xlsx"
    assert paths[b] == f"data/sales_{b.hex[:8]}.xlsx"
    assert paths[c] == "data/other_file.csv"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("outputs/summary.csv", True),
        ("outputs/charts/trend.png", True),
        ("output_2026-10-04-03-12-01-424102.png", True),
        ("scratch.tmp", False),
        ("data/sales.csv", False),
        ("outputs", False),
        ("outputs/../secret", False),
        ("sub/output_1.png", False),
        ("output_1.svg", False),
        ("outputs/.keep", False),
    ],
)
def test_artifact_filter(name, expected):
    assert is_artifact_path(name) is expected


def test_clean_stderr_strips_ansi_and_the_entry_point_frame():
    raw = (
        "boom\nTraceback (most recent call last):\n"
        '  File "/usr/bin/entry/entry_point", line 109, in _run_python\n'
        "    exec(code, exec_scope)  # pylint: disable=exec-used\n"
        "    ^^^^^^^^^^^^^^^^^^^^^^\n"
        '  File "<string>", line 1, in <module>\n'
        "\x1b[31mValueError\x1b[0m: boom\n"
    )
    assert clean_stderr(raw) == (
        'boom\nTraceback (most recent call last):\n  File "<string>", line 1, in <module>\nValueError: boom\n'
    )


# --- workspace (DB) -------------------------------------------------------------------------------------


async def no_sleep(_):
    pass


@pytest.fixture
def storage():
    return FakeStorage()


@pytest.fixture
def provider():
    return FakeSandboxProvider()


@pytest.fixture
def manager(async_redis_db, provider):
    return SandboxManager(
        provider, SandboxSlots(async_redis_db, 5), ttl_s=3600, idle_timeout_s=300, exec_timeout_s=1, sleep=no_sleep
    )


@pytest.fixture
def session_factory(db):
    @contextmanager
    def factory():
        yield db  # the test's session; not closed so the test transaction stays usable

    return factory


@pytest.fixture
def conversation(db, user):
    conv = Conversation(user_id=user.id, model="anthropic:claude-sonnet-5-5")
    db.add(conv)
    db.flush()
    return conv


@pytest.fixture
def run(db, user, conversation):
    row = Run(conversation_id=conversation.id, user_id=user.id, status="running")
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def add_dataset(db, storage):
    def _add(owner, filename, data=b"a,b\n1,2\n", status="ready", attach_to=None):
        dataset = Dataset(
            user_id=owner.id,
            filename=filename,
            gcs_key=f"users/{owner.id}/datasets/{uuid.uuid4()}/{filename}",
            size_bytes=len(data),
            content_type="text/csv",
            status=status,
        )
        db.add(dataset)
        db.flush()
        storage.objects[dataset.gcs_key] = data
        if attach_to is not None:
            db.add(ConversationDataset(conversation_id=attach_to.id, dataset_id=dataset.id))
        db.commit()
        return dataset

    return _add


@pytest.fixture
def events():
    return []


@pytest.fixture
def workspace(manager, storage, session_factory, user, conversation, run, events):
    async def emit(event_type, payload):
        events.append((event_type, payload))

    return ConversationWorkspace(
        user_id=user.id,
        conversation_id=conversation.id,
        run_id=run.id,
        manager=manager,
        storage=storage,
        session_factory=session_factory,
        emit=emit,
    )


def user_code(sandbox):
    return [c for c in sandbox.executed if not c.startswith("# sandbox housekeeping")]


def sent_datasets(sandbox):
    return [name for name in sandbox.received if name.startswith("data/")]


async def test_run_python_creates_sandbox_and_reuses_it(workspace, provider, conversation, db, events):
    result = await workspace.run_python("1 + 1")
    assert not result.is_error and "ran: 1 + 1" in result.output
    db.refresh(conversation)
    assert conversation.sandbox_id == "sbx-1"
    assert provider.created[0].display_name == f"conversation-{conversation.id}"
    assert ("status", {"message": "Starting sandbox…"}) in events

    await workspace.run_python("2 + 2")
    assert len(provider.created) == 1
    assert user_code(provider.created[0]) == ["1 + 1", "2 + 2"]
    assert len(provider.created[0].executed) == 2  # no datasets: no housekeeping execution
    assert provider.created[0].received == ["outputs/.keep", "outputs/.keep"]


async def test_hydrates_only_attached_ready_datasets_of_the_owner(
    workspace, provider, conversation, user, make_user, add_dataset, events, async_redis_db
):
    sales = add_dataset(user, "sales.csv", b"sales", attach_to=conversation)
    add_dataset(user, "pending.csv", status="pending", attach_to=conversation)
    add_dataset(user, "unattached.csv")
    # Not possible through the API (attach checks ownership), but hydration must not trust the link table.
    add_dataset(make_user("auth0|intruder"), "secret.csv", b"secret", attach_to=conversation)

    await workspace.run_python("pass")
    sandbox = provider.created[0]
    assert {k: v for k, v in sandbox.files.items() if k.startswith("data/")} == {"data/sales.csv": b"sales"}
    assert ("status", {"message": "Loading 1 dataset(s) into the sandbox…"}) in events
    assert await async_redis_db.hgetall(datasets_key("sbx-1")) == {str(sales.id): "data/sales.csv"}

    await workspace.run_python("pass")
    assert sent_datasets(sandbox) == ["data/sales.csv"]  # files persist: not sent again


async def test_newly_attached_dataset_is_sent_and_detached_one_removed(
    workspace, provider, conversation, user, add_dataset, db, async_redis_db
):
    first = add_dataset(user, "first.csv", b"1", attach_to=conversation)
    await workspace.run_python("pass")

    second = add_dataset(user, "second.csv", b"2", attach_to=conversation)
    db.query(ConversationDataset).filter_by(dataset_id=first.id).delete()
    db.commit()
    await workspace.run_python("pass")

    sandbox = provider.created[0]
    assert {k for k in sandbox.files if k.startswith("data/")} == {"data/second.csv"}
    assert sent_datasets(sandbox) == ["data/first.csv", "data/second.csv"]
    assert await async_redis_db.hgetall(datasets_key("sbx-1")) == {str(second.id): "data/second.csv"}


async def test_a_path_freed_by_detach_is_not_removed_when_reused(
    workspace, provider, conversation, user, add_dataset, db
):
    old = add_dataset(user, "sales.csv", b"old", attach_to=conversation)
    await workspace.run_python("pass")
    db.query(ConversationDataset).filter_by(dataset_id=old.id).delete()
    add_dataset(user, "sales.csv", b"new", attach_to=conversation)
    db.commit()

    await workspace.run_python("pass")

    assert provider.created[0].files["data/sales.csv"] == b"new"


async def test_large_datasets_are_sent_in_batches(workspace, provider, conversation, user, add_dataset, monkeypatch):
    monkeypatch.setattr("app.agent.workspace.HYDRATE_BATCH_BYTES", 10)
    for name in ("a.csv", "b.csv", "c.csv"):
        add_dataset(user, name, b"x" * 6, attach_to=conversation)

    await workspace.run_python("pass")

    sandbox = provider.created[0]
    housekeeping = [c for c in sandbox.executed if c.startswith("# sandbox housekeeping")]
    assert len(housekeeping) == 3
    assert sent_datasets(sandbox) == ["data/a.csv", "data/b.csv", "data/c.csv"]


async def test_expired_sandbox_is_rehydrated_and_the_agent_told(
    workspace, provider, conversation, user, add_dataset, db, async_redis_db
):
    add_dataset(user, "sales.csv", b"sales", attach_to=conversation)
    first = await workspace.run_python("x = 1")
    assert RESET_NOTE not in first.output  # a brand-new sandbox isn't a reset

    provider.expire("sbx-1")
    await async_redis_db.zadd(LEASES_KEY, {"sbx-1": time.time() - 1})
    result = await workspace.run_python("print(x)")

    assert result.output.startswith(RESET_NOTE)
    assert provider.created[1].files["data/sales.csv"] == b"sales"
    db.refresh(conversation)
    assert conversation.sandbox_id == "sbx-2"


async def test_sandbox_lost_mid_turn_is_replaced_and_the_code_rerun(
    workspace, provider, conversation, user, add_dataset, db
):
    add_dataset(user, "sales.csv", b"sales", attach_to=conversation)
    await workspace.run_python("warm up")

    async def expire_then_run(sandbox, code):
        if sandbox.name == "sbx-1":
            provider.expire(sandbox.name)
            raise SandboxGone
        return ExecResult(0, stdout="second sandbox\n")

    provider.handler = expire_then_run
    result = await workspace.run_python("analyse()")

    assert not result.is_error
    assert result.output.startswith(RESET_NOTE) and "second sandbox" in result.output
    assert provider.created[1].files["data/sales.csv"] == b"sales"
    db.refresh(conversation)
    assert conversation.sandbox_id == "sbx-2"


async def test_outputs_and_figures_become_artifacts_scratch_files_do_not(
    workspace, provider, storage, events, db, run, user
):
    async def handler(sandbox, code):
        return ExecResult(
            0,
            stdout="done\n",
            files=[
                OutputFile("output_2026-10-04-03-12-01-424102.png", b"\x89PNG-figure"),
                OutputFile("scratch.tmp", b"ignored"),
                OutputFile("outputs/summary report.csv", b"x,y\n"),
                OutputFile("outputs/charts/trend.svg", b"<svg/>"),
            ],
        )

    provider.handler = handler
    result = await workspace.run_python("plot()")

    assert [(a.filename, a.kind) for a in result.artifacts] == [
        ("figure-1.png", "image"),
        ("summary_report.csv", "file"),
        ("trend.svg", "image"),
    ]
    rows = {r.id: r for r in db.scalars(select(Artifact).where(Artifact.run_id == run.id))}
    assert set(rows) == {a.id for a in result.artifacts}
    for artifact in result.artifacts:
        row = rows[artifact.id]
        assert row.user_id == user.id
        assert row.gcs_key == f"users/{user.id}/artifacts/{run.id}/{row.id}/{row.filename}"
        assert row.gcs_key in storage.objects
    content_types = {rows[a.id].filename: storage.content_types[rows[a.id].gcs_key] for a in result.artifacts}
    assert content_types == {"figure-1.png": "image/png", "summary_report.csv": "text/csv", "trend.svg": "image/svg+xml"}
    assert b"ignored" not in storage.objects.values()
    assert [e for e in events if e[0] == "artifact"] == [
        ("artifact", {"artifact_id": str(a.id), "kind": a.kind, "filename": a.filename}) for a in result.artifacts
    ]
    assert "Saved for the user: figure-1.png, summary_report.csv, trend.svg" in result.output


async def test_artifact_size_and_count_caps(workspace, provider, storage, monkeypatch, db, run):
    monkeypatch.setattr("app.agent.workspace.MAX_ARTIFACT_BYTES", 10)

    async def handler(sandbox, code):
        files = [OutputFile("outputs/big.bin", b"x" * 11)]
        files += [OutputFile(f"outputs/f{i:02d}.txt", b"ok") for i in range(MAX_ARTIFACTS_PER_EXEC + 2)]
        return ExecResult(0, files=files)

    provider.handler = handler
    result = await workspace.run_python("go()")

    assert len(result.artifacts) == MAX_ARTIFACTS_PER_EXEC
    assert len(db.scalars(select(Artifact.id).where(Artifact.run_id == run.id)).all()) == MAX_ARTIFACTS_PER_EXEC
    assert len(storage.objects) == MAX_ARTIFACTS_PER_EXEC
    assert "outputs/big.bin was not saved: larger than 10 bytes." in result.output
    assert f"Not saved (limit {MAX_ARTIFACTS_PER_EXEC} files per execution): outputs/f10.txt, outputs/f11.txt" in (
        result.output
    )


async def test_timeout_rolls_back_and_keeps_the_sandbox(workspace, provider, conversation, db):
    await workspace.run_python("x = 1")
    sandbox = provider.created[0]
    sandbox.state["x"] = 1

    async def runaway(sb, code):
        sb.state["x"] = 2
        await asyncio.sleep(10)

    provider.handler = runaway
    result = await workspace.run_python("x = 2; while True: pass")

    assert result.is_error
    assert result.output == (
        "Error: execution exceeded the 1 s time limit and was rolled back; variables and files are as before."
    )
    assert sandbox.state == {"x": 1}
    assert provider.deleted == []  # no reset needed
    provider.handler = FakeSandboxProvider.default_handler
    after = await workspace.run_python("print(x)")
    assert not after.is_error and RESET_NOTE not in after.output
    assert len(provider.created) == 1


async def test_cancel_aborts_the_execution_and_leaves_the_sandbox_usable(workspace, provider):
    await workspace.run_python("warm up")
    started = asyncio.Event()

    async def hang(sandbox, code):
        sandbox.files["outputs/partial.csv"] = b"half"
        started.set()
        await asyncio.Event().wait()

    provider.handler = hang
    task = asyncio.create_task(workspace.run_python("import time; time.sleep(1e9)"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    sandbox = provider.created[0]
    assert "outputs/partial.csv" not in sandbox.files  # rolled back
    assert provider.deleted == []
    provider.handler = FakeSandboxProvider.default_handler
    assert not (await workspace.run_python("1")).is_error  # the lock was released


async def test_parallel_tool_calls_are_serialized(workspace, provider):
    async def slow(sandbox, code):
        await asyncio.sleep(0.01)
        return ExecResult(0, stdout=code)

    provider.handler = slow
    results = await asyncio.gather(*(workspace.run_python(f"call {i}") for i in range(5)))

    assert all(not r.is_error for r in results)
    assert provider.max_parallel == 1


async def test_errors_are_reported_cleaned_and_truncated(workspace, provider):
    async def failing(sandbox, code):
        return ExecResult(
            106,
            stdout="x" * 50_000,
            stderr=(
                "'col'\nTraceback (most recent call last):\n"
                '  File "/usr/bin/entry/entry_point", line 109, in _run_python\n'
                "    exec(code, exec_scope)\n"
                "\x1b[0;31mKeyError\x1b[0m: 'col'\n"
            ),
        )

    provider.handler = failing
    result = await workspace.run_python("df['col']")

    assert result.is_error
    assert "KeyError: 'col'" in result.output
    assert "\x1b" not in result.output and "entry_point" not in result.output
    assert "[40000 characters truncated]" in result.output


async def test_nonzero_exit_without_stderr_is_still_an_error(workspace, provider):
    async def silent_failure(sandbox, code):
        return ExecResult(101)

    provider.handler = silent_failure
    result = await workspace.run_python("import missing")
    assert result.is_error and "error: exit status 101" in result.output


async def test_capacity_busy_is_reported_to_the_agent(workspace, manager, async_redis_db):
    manager.slots._capacity = 0
    manager._busy_wait_s = 0
    result = await workspace.run_python("1")
    assert result.is_error and "no code sandbox is available" in result.output


@pytest.mark.parametrize("problem", ["missing", "oversize"])
async def test_unreadable_dataset_is_skipped_noted_and_retried(
    workspace, provider, conversation, user, add_dataset, storage, monkeypatch, async_redis_db, problem
):
    good = add_dataset(user, "good.csv", b"good", attach_to=conversation)
    bad = add_dataset(user, "bad.csv", b"bad-data", attach_to=conversation)
    original = storage.objects[bad.gcs_key]
    if problem == "missing":
        del storage.objects[bad.gcs_key]
    else:
        monkeypatch.setattr("app.agent.workspace.MAX_UPLOAD_BYTES", 5)

    result = await workspace.run_python("analyse()")

    assert not result.is_error and "ran: analyse()" in result.output
    assert "Note: dataset data/bad.csv could not be loaded into the sandbox" in result.output
    sandbox = provider.created[0]
    assert sent_datasets(sandbox) == ["data/good.csv"]
    assert await async_redis_db.hgetall(datasets_key("sbx-1")) == {str(good.id): "data/good.csv"}

    storage.objects[bad.gcs_key] = original
    monkeypatch.setattr("app.agent.workspace.MAX_UPLOAD_BYTES", 1024)
    retried = await workspace.run_python("analyse()")
    assert "could not be loaded" not in retried.output
    assert sent_datasets(sandbox) == ["data/good.csv", "data/bad.csv"]


async def test_agent_engine_outage_becomes_an_error_result(workspace, provider):
    async def outage(ttl_s, display_name):
        raise SandboxUnavailableError("create: 503 UNAVAILABLE")

    provider.create = outage
    result = await workspace.run_python("1")
    assert result.is_error and "temporarily unavailable" in result.output


def test_housekeeping_removes_by_absolute_path():
    code = housekeeping_code(["data/old.csv"])
    assert "os.path.join('/home/bard', _p)" in code and "_remove = ['data/old.csv']" in code


def wrap_housekeeping(provider, replacement):
    """Routes housekeeping executions to `replacement(name, code, files, timeout_s)`."""
    original = provider.execute

    async def execute(name, code, files, timeout_s):
        if code.startswith("# sandbox housekeeping"):
            return await replacement(original, name, code, files, timeout_s)
        return await original(name, code, files, timeout_s)

    provider.execute = execute


async def test_artifact_upload_failure_is_reported_not_raised(workspace, provider, storage, monkeypatch, events):
    def broken_upload(key, data, content_type):
        if key.endswith("broken.csv"):
            raise ServiceUnavailable("gcs down")
        storage.objects[key] = data

    monkeypatch.setattr(storage, "upload", broken_upload)

    async def handler(sandbox, code):
        return ExecResult(
            0, stdout="done\n", files=[OutputFile("outputs/broken.csv", b"x"), OutputFile("outputs/ok.csv", b"y")]
        )

    provider.handler = handler
    result = await workspace.run_python("save()")

    assert not result.is_error and "done" in result.output
    assert [a.filename for a in result.artifacts] == ["ok.csv"]
    assert "outputs/broken.csv could not be saved for the user (storage error)" in result.output
    assert [e[1]["filename"] for e in events if e[0] == "artifact"] == ["ok.csv"]


async def test_dataset_upload_timeout_is_a_dataset_note_not_a_code_timeout(
    workspace, provider, conversation, user, add_dataset, manager, async_redis_db
):
    add_dataset(user, "big.csv", b"big", attach_to=conversation)
    deadlines = []

    async def slow_upload(original, name, code, files, timeout_s):
        deadlines.append(timeout_s)
        raise ExecTimeout

    wrap_housekeeping(provider, slow_upload)
    result = await workspace.run_python("analyse()")

    assert deadlines == [manager.housekeeping_timeout_s]  # its own, longer deadline
    assert not result.is_error and "ran: analyse()" in result.output
    assert "exceeded" not in result.output
    assert "Note: dataset data/big.csv could not be loaded into the sandbox (upload timed out)" in result.output
    assert await async_redis_db.hgetall(datasets_key("sbx-1")) == {}  # retried next call


async def test_failing_housekeeping_is_reported_as_unavailable(workspace, provider, conversation, user, add_dataset):
    add_dataset(user, "a.csv", attach_to=conversation)

    async def broken(original, name, code, files, timeout_s):
        return ExecResult(1, stderr="disk full")

    wrap_housekeeping(provider, broken)
    result = await workspace.run_python("1")
    assert result.is_error and "temporarily unavailable" in result.output


async def test_conversation_deleted_mid_run_deletes_the_new_sandbox(workspace, provider, monkeypatch):
    monkeypatch.setattr(workspace, "_save_sandbox_id", lambda sandbox_id, expected: False)
    with pytest.raises(ConversationGoneError):
        await workspace.run_python("1")
    assert provider.deleted == ["sbx-1"]


async def test_new_sandbox_vanishing_during_first_hydration_is_not_a_reset(
    workspace, provider, conversation, user, add_dataset
):
    add_dataset(user, "sales.csv", b"sales", attach_to=conversation)

    async def vanish_first(original, name, code, files, timeout_s):
        if name == "sbx-1":
            provider.expire(name)
            raise SandboxGone
        return await original(name, code, files, timeout_s)

    wrap_housekeeping(provider, vanish_first)
    result = await workspace.run_python("analyse()")

    assert not result.is_error and RESET_NOTE not in result.output
    assert provider.created[1].files["data/sales.csv"] == b"sales"
