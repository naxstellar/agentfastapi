import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import google.auth.exceptions
import httpx
import pytest
from google.genai import errors

from app.agent import sandbox as sandbox_module
from app.agent.sandbox import (
    LAST_USED_KEY,
    LEASES_KEY,
    AgentEngineProvider,
    ConcurrentExecutionError,
    ExecResult,
    ExecTimeout,
    InputFile,
    SandboxBusyError,
    SandboxGone,
    SandboxManager,
    SandboxSlots,
    SandboxUnavailableError,
    datasets_key,
    delete_sandbox_sync,
    parse_execute_response,
)
from tests.fakes import FakeSandboxProvider


async def no_sleep(_):
    pass


def make_manager(redis, provider, capacity=5, **kwargs) -> SandboxManager:
    kwargs.setdefault("sleep", no_sleep)
    return SandboxManager(
        provider, SandboxSlots(redis, capacity), ttl_s=3600, idle_timeout_s=300, exec_timeout_s=120, **kwargs
    )


# --- slots ----------------------------------------------------------------------------------------------


async def test_slots_cap_live_sandboxes_and_expired_leases_free_up(async_redis_db):
    slots = SandboxSlots(async_redis_db, capacity=2)
    later = time.time() + 600
    assert await slots.acquire("a", later)
    assert await slots.acquire("b", later)
    assert not await slots.acquire("c", later)

    await slots.release("a")
    assert await slots.acquire("c", later)

    # A sandbox that reached its TTL frees its slot once its lease runs out, without any callback.
    await async_redis_db.zadd(LEASES_KEY, {"b": time.time() - 1})
    assert await slots.acquire("d", later)
    assert set(await async_redis_db.zrange(LEASES_KEY, 0, -1)) == {"c", "d"}


async def test_release_forgets_lease_activity_and_sent_datasets(async_redis_db):
    slots = SandboxSlots(async_redis_db, capacity=2)
    await slots.acquire("a", time.time() + 600)
    await slots.touch("a", time.time())
    await async_redis_db.hset(datasets_key("a"), "d1", "data/x.csv")

    await slots.release("a")

    assert await async_redis_db.zcard(LEASES_KEY) == 0
    assert await async_redis_db.zcard(LAST_USED_KEY) == 0
    assert not await async_redis_db.exists(datasets_key("a"))


# --- manager --------------------------------------------------------------------------------------------


async def test_ensure_creates_then_reuses(async_redis_db):
    provider = FakeSandboxProvider()
    manager = make_manager(async_redis_db, provider)

    handle, fresh = await manager.ensure(None, "conversation-1")
    assert fresh and provider.created[0].display_name == "conversation-1"
    assert await manager.slots.expires_at(handle.name) == pytest.approx(handle.expires_at)

    again, fresh = await manager.ensure(handle.name, "conversation-1")
    assert again.name == handle.name and not fresh
    assert len(provider.created) == 1


async def test_ensure_replaces_expired_sandbox(async_redis_db):
    provider = FakeSandboxProvider()
    manager = make_manager(async_redis_db, provider)
    handle, _ = await manager.ensure(None, "c")
    provider.expire(handle.name)
    await async_redis_db.zadd(LEASES_KEY, {handle.name: time.time() - 1})

    replacement, fresh = await manager.ensure(handle.name, "c")

    assert fresh and replacement.name != handle.name
    assert await async_redis_db.zrange(LEASES_KEY, 0, -1) == [replacement.name]


async def test_ensure_recycles_a_sandbox_about_to_expire(async_redis_db):
    # The TTL is fixed at creation, so a sandbox near its end is replaced before a turn, not during one.
    provider = FakeSandboxProvider()
    manager = make_manager(async_redis_db, provider)
    handle, _ = await manager.ensure(None, "c")
    await manager.slots.renew(handle.name, time.time() + manager.min_remaining_s - 1)

    replacement, fresh = await manager.ensure(handle.name, "c")

    assert fresh and replacement.name != handle.name
    assert provider.deleted == [handle.name]


async def test_ensure_asks_the_service_when_redis_lost_track(async_redis_db):
    provider = FakeSandboxProvider()
    manager = make_manager(async_redis_db, provider)
    handle, _ = await manager.ensure(None, "c")
    await async_redis_db.flushdb()

    again, fresh = await manager.ensure(handle.name, "c")
    assert again.name == handle.name and not fresh
    assert await manager.slots.expires_at(handle.name) == pytest.approx(handle.expires_at)

    await async_redis_db.flushdb()
    provider.expire(handle.name)
    replacement, fresh = await manager.ensure(handle.name, "c")
    assert fresh and replacement.name != handle.name


async def test_ensure_waits_for_capacity_then_gives_up(async_redis_db):
    provider = FakeSandboxProvider()
    manager = make_manager(async_redis_db, provider, capacity=1, busy_wait_s=5)
    first, _ = await manager.ensure(None, "a")
    await manager.slots.touch(first.name, time.time())  # in use, so not reapable

    statuses = []

    async def on_status(message):
        statuses.append(message)

    with pytest.raises(SandboxBusyError):
        await manager.ensure(None, "b", on_status=on_status)
    assert statuses == ["Sandbox capacity is busy, retrying…"]  # once, not per retry
    assert len(provider.created) == 1


async def test_ensure_gets_a_slot_freed_while_waiting(async_redis_db):
    provider = FakeSandboxProvider()
    first, _ = await make_manager(async_redis_db, provider, capacity=1).ensure(None, "a")

    async def free_slot(_):
        await async_redis_db.zrem(LEASES_KEY, first.name)

    manager = make_manager(async_redis_db, provider, capacity=1, sleep=free_slot)
    handle, fresh = await manager.ensure(None, "b")
    assert fresh and handle.name != first.name


async def test_full_capacity_reaps_idle_sandboxes_first(async_redis_db):
    provider = FakeSandboxProvider()
    manager = make_manager(async_redis_db, provider, capacity=1, busy_wait_s=0)
    idle, _ = await manager.ensure(None, "a")
    await manager.slots.touch(idle.name, time.time() - 301)

    handle, fresh = await manager.ensure(None, "b")

    assert fresh and provider.deleted == [idle.name]
    assert await async_redis_db.zrange(LEASES_KEY, 0, -1) == [handle.name]


async def test_reap_idle_skips_busy_and_recent_sandboxes(async_redis_db):
    provider = FakeSandboxProvider()
    manager = make_manager(async_redis_db, provider)
    idle, _ = await manager.ensure(None, "a")
    recent, _ = await manager.ensure(None, "b")
    busy, _ = await manager.ensure(None, "c")
    expired, _ = await manager.ensure(None, "d")
    now = time.time()
    await manager.slots.touch(idle.name, now - 301)
    await manager.slots.touch(recent.name, now - 10)
    await manager.slots.touch(busy.name, now + 100)  # executing: marked busy until its deadline
    await manager.slots.touch(expired.name, now - 301)
    await manager.slots.renew(expired.name, now - 60)  # already gone by TTL: nothing to delete

    assert await manager.reap_idle() == 1

    assert provider.deleted == [idle.name]
    assert set(await async_redis_db.zrange(LAST_USED_KEY, 0, -1)) == {recent.name, busy.name}
    assert await manager.slots.expires_at(idle.name) is None


async def test_failed_create_releases_its_slot(async_redis_db):
    provider = FakeSandboxProvider()

    async def broken_create(ttl_s, display_name):
        raise SandboxUnavailableError("quota")

    provider.create = broken_create
    with pytest.raises(SandboxUnavailableError):
        await make_manager(async_redis_db, provider).ensure(None, "c")
    assert await async_redis_db.zcard(LEASES_KEY) == 0


async def test_cancel_during_create_deletes_the_orphan(async_redis_db):
    provider = FakeSandboxProvider()
    provider.create_delay = 0.05
    manager = make_manager(async_redis_db, provider)

    task = asyncio.create_task(manager.ensure(None, "c"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)

    assert provider.deleted == ["sbx-1"]
    assert await async_redis_db.zcard(LEASES_KEY) == 0


async def test_execute_marks_activity_and_releases_a_gone_sandbox(async_redis_db):
    provider = FakeSandboxProvider()
    manager = make_manager(async_redis_db, provider)
    handle, _ = await manager.ensure(None, "c")

    before = time.time()
    result = await manager.execute(handle, "1 + 1")
    assert result.stdout == "ran: 1 + 1\n"
    assert await async_redis_db.zscore(LAST_USED_KEY, handle.name) >= before

    provider.expire(handle.name)
    with pytest.raises(SandboxGone):
        await manager.execute(handle, "2")
    assert await manager.slots.expires_at(handle.name) is None
    assert await async_redis_db.zscore(LAST_USED_KEY, handle.name) is None


async def test_execute_retries_a_concurrent_execution_rejection_once(async_redis_db):
    provider = FakeSandboxProvider()
    manager = make_manager(async_redis_db, provider)
    handle, _ = await manager.ensure(None, "c")
    calls = []
    original = provider.execute

    async def flaky(name, code, files, timeout_s):
        calls.append(code)
        if len(calls) == 1:
            raise ConcurrentExecutionError
        return await original(name, code, files, timeout_s)

    provider.execute = flaky
    assert (await manager.execute(handle, "x")).exit_status == 0
    assert calls == ["x", "x"]

    async def always_busy(name, code, files, timeout_s):
        raise ConcurrentExecutionError

    provider.execute = always_busy
    with pytest.raises(SandboxUnavailableError):
        await manager.execute(handle, "x")


def test_delete_sandbox_sync_forgets_redis_state(redis_db, monkeypatch):
    monkeypatch.setattr(sandbox_module, "AGENT_ENGINE_NAME", None)
    redis_db.zadd(LEASES_KEY, {"sbx": time.time() + 60})
    redis_db.zadd(LAST_USED_KEY, {"sbx": time.time()})
    redis_db.hset(datasets_key("sbx"), "d", "data/x.csv")

    delete_sandbox_sync("sbx", redis_db)
    delete_sandbox_sync("sbx", redis_db)  # idempotent

    assert redis_db.zcard(LEASES_KEY) == 0 and redis_db.zcard(LAST_USED_KEY) == 0
    assert not redis_db.exists(datasets_key("sbx"))


# --- Agent Engine adapter (fake SDK client) ---------------------------------------------------------------

ENGINE = "projects/123/locations/asia-south1/reasoningEngines/456"
SANDBOX = f"{ENGINE}/sandboxEnvironments/789"


def chunk(data: bytes, mime_type=None, file_name=None):
    metadata = SimpleNamespace(attributes={"file_name": file_name.encode()}) if file_name else None
    return SimpleNamespace(data=data, mime_type=mime_type, metadata=metadata)


def result_chunk(exit_status=0, out="", err=""):
    payload = {"exit_status_int": exit_status, "msg_out": out, "msg_err": err}
    return chunk(json.dumps(payload).encode(), "application/json")


class FakeSdkClient:
    def __init__(self):
        self.execute_impl = None
        self.get_impl = lambda name: SimpleNamespace(state="SandboxState.STATE_RUNNING", expire_time=None)
        self.requests = []
        self.create_impl = None  # raises instead of starting a create operation, if set
        # Operation states returned by successive polls after _create (which returns a pending one).
        self.operation_polls = [SimpleNamespace(name="operations/1", done=True, error=None, response=SimpleNamespace(name=SANDBOX))]
        self.polled = 0
        self.aio = SimpleNamespace(
            sandboxes=SimpleNamespace(
                _execute_code=self._execute_code,
                _create=self._create,
                _get_sandbox_operation=self._get_sandbox_operation,
                _get=self._aget,
            )
        )
        self.sandboxes = SimpleNamespace(get=lambda name: self.get_impl(name), delete=self._delete)
        self.deleted = []

    async def _execute_code(self, *, name, inputs, config):
        self.requests.append((name, inputs, config))
        return await self.execute_impl()

    async def _create(self, *, name, spec, config):
        if self.create_impl is not None:
            self.create_impl()
        self.create_args = (name, spec, config)
        return SimpleNamespace(name="operations/1", done=False, error=None, response=None)

    async def _get_sandbox_operation(self, *, operation_name):
        self.polled += 1
        return self.operation_polls[min(self.polled, len(self.operation_polls)) - 1]

    async def _aget(self, *, name):
        return SimpleNamespace(name=name, expire_time=datetime.fromtimestamp(2_000_000_000, timezone.utc))

    def _delete(self, name):
        self.deleted.append(name)


def api_error(code, status):
    cls = errors.ClientError if code < 500 else errors.ServerError
    return cls(code, {"error": {"code": code, "status": status, "message": "x"}})


@pytest.fixture
def sdk():
    return FakeSdkClient()


@pytest.fixture
def provider(sdk):
    return AgentEngineProvider(ENGINE, client=sdk)


async def test_execute_sends_code_and_files_and_parses_the_response(provider, sdk):
    async def respond():
        return SimpleNamespace(
            outputs=[
                result_chunk(0, "hi\n", ""),
                chunk(b"png", None, "output_1.png"),
                chunk(b"csv", "application/vnd.ms-excel", "outputs/a.csv"),
            ]
        )

    sdk.execute_impl = respond
    result = await provider.execute(SANDBOX, "print('hi')", [InputFile("data/x.csv", b"1,2")], 30)

    assert result == ExecResult(0, "hi\n", "", [result.files[0], result.files[1]])
    assert [(f.name, f.data) for f in result.files] == [("output_1.png", b"png"), ("outputs/a.csv", b"csv")]
    name, inputs, config = sdk.requests[0]
    assert name == SANDBOX
    assert json.loads(inputs[0].data) == {"code": "print('hi')"}
    assert inputs[1].data == b"1,2" and inputs[1].metadata.attributes["file_name"] == b"data/x.csv"
    assert config["http_options"]["timeout"] == 35_000


def test_parse_rejects_a_response_without_result_chunk():
    with pytest.raises(SandboxUnavailableError):
        parse_execute_response(SimpleNamespace(outputs=[chunk(b"x", None, "a.txt")]))


async def test_asyncio_deadline_cancels_the_call(provider, sdk):
    cancelled = asyncio.Event()

    async def hang():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    sdk.execute_impl = hang
    with pytest.raises(ExecTimeout):
        await provider.execute(SANDBOX, "while True: pass", [], 0.05)
    assert cancelled.is_set()  # the HTTP request is aborted, so the service rolls the execution back


@pytest.mark.parametrize(
    ("error", "get_result", "expected"),
    [
        (api_error(400, "FAILED_PRECONDITION"), api_error(404, "NOT_FOUND"), SandboxGone),
        (api_error(400, "FAILED_PRECONDITION"), "running", ConcurrentExecutionError),
        (api_error(504, "DEADLINE_EXCEEDED"), "running", ExecTimeout),
        (api_error(503, "UNAVAILABLE"), "running", SandboxUnavailableError),  # fast 503: an outage
        (api_error(500, "INTERNAL"), api_error(404, "NOT_FOUND"), SandboxGone),
        (httpx.ConnectError("down"), "running", SandboxUnavailableError),
    ],
)
async def test_error_mapping(provider, sdk, error, get_result, expected):
    async def fail():
        raise error

    def get(name):
        if isinstance(get_result, Exception):
            raise get_result
        return SimpleNamespace(state="SandboxState.STATE_RUNNING", expire_time=None)

    sdk.execute_impl = fail
    sdk.get_impl = get
    with pytest.raises(expected):
        await provider.execute(SANDBOX, "x", [], 30)


async def test_late_503_is_the_server_cap_and_maps_to_timeout(provider, sdk, monkeypatch):
    clock = iter([0.0, 300.0])
    # Only the module's own clock: asyncio.timeout reads time.monotonic too.
    monkeypatch.setattr(sandbox_module, "time", SimpleNamespace(monotonic=lambda: next(clock), time=time.time))

    async def fail():
        raise api_error(503, "UNAVAILABLE")

    sdk.execute_impl = fail
    with pytest.raises(ExecTimeout):
        await provider.execute(SANDBOX, "x", [], 289)


async def test_delete_ignores_missing_sandbox(provider, sdk):
    def gone(name):
        raise api_error(404, "NOT_FOUND")

    sdk.sandboxes.delete = gone
    await provider.delete(SANDBOX)


# --- service failures never escape as raw SDK/network errors ----------------------------------------------


def raiser(exc):
    def _raise(*args, **kwargs):
        raise exc

    return _raise


@pytest.mark.parametrize(
    "error",
    [
        api_error(429, "RESOURCE_EXHAUSTED"),
        api_error(500, "INTERNAL"),
        httpx.ConnectError("down"),
        google.auth.exceptions.RefreshError("token"),
    ],
)
async def test_create_failures_map_to_unavailable(provider, sdk, error):
    sdk.create_impl = raiser(error)
    with pytest.raises(SandboxUnavailableError):
        await provider.create(600, "c")


@pytest.mark.parametrize("error", [api_error(500, "INTERNAL"), api_error(403, "PERMISSION_DENIED"), httpx.ReadError("x")])
async def test_get_failures_map_to_unavailable_but_404_means_gone(provider, sdk, error):
    sdk.get_impl = raiser(error)
    with pytest.raises(SandboxUnavailableError):
        await provider.expires_at(SANDBOX)

    sdk.get_impl = raiser(api_error(404, "NOT_FOUND"))
    assert await provider.expires_at(SANDBOX) is None


@pytest.mark.parametrize("get_error", [api_error(503, "UNAVAILABLE"), httpx.ConnectError("down")])
async def test_failing_gone_check_maps_to_unavailable(provider, sdk, get_error):
    async def fail():
        raise api_error(400, "FAILED_PRECONDITION")

    sdk.execute_impl = fail
    sdk.get_impl = raiser(get_error)
    with pytest.raises(SandboxUnavailableError):
        await provider.execute(SANDBOX, "x", [], 30)


async def test_credential_refresh_failure_during_execute_maps_to_unavailable(provider, sdk):
    async def fail():
        raise google.auth.exceptions.TransportError("metadata server down")

    sdk.execute_impl = fail
    with pytest.raises(SandboxUnavailableError):
        await provider.execute(SANDBOX, "x", [], 30)


async def test_delete_failure_maps_to_unavailable(provider, sdk):
    sdk.sandboxes.delete = raiser(api_error(500, "INTERNAL"))
    with pytest.raises(SandboxUnavailableError):
        await provider.delete(SANDBOX)


async def test_failed_recycle_delete_does_not_fail_the_turn(async_redis_db):
    provider = FakeSandboxProvider()
    manager = make_manager(async_redis_db, provider)
    handle, _ = await manager.ensure(None, "c")
    await manager.slots.renew(handle.name, time.time() + manager.min_remaining_s - 1)

    async def broken_delete(name):
        raise SandboxUnavailableError("delete: 500 INTERNAL")

    provider.delete = broken_delete
    replacement, fresh = await manager.ensure(handle.name, "c")

    assert fresh and replacement.name != handle.name
    assert await manager.slots.expires_at(handle.name) is None


# --- create: async, polled, bounded -----------------------------------------------------------------------


async def test_create_polls_the_operation_then_reads_the_sandbox(provider, sdk, monkeypatch):
    monkeypatch.setattr(sandbox_module, "CREATE_POLL_S", 0)
    pending = SimpleNamespace(name="operations/1", done=False, error=None, response=None)
    sdk.operation_polls = [pending, pending, sdk.operation_polls[0]]

    handle = await provider.create(600, "conversation-1")

    assert handle.name == SANDBOX and handle.expires_at == 2_000_000_000
    assert sdk.polled == 3
    name, spec, config = sdk.create_args
    assert name == ENGINE and spec == {"code_execution_environment": {"code_language": "LANGUAGE_PYTHON"}}
    assert config.ttl == "600s" and config.display_name == "conversation-1"


async def test_failed_create_operation_maps_to_unavailable(provider, sdk, monkeypatch):
    monkeypatch.setattr(sandbox_module, "CREATE_POLL_S", 0)
    sdk.operation_polls = [SimpleNamespace(name="operations/1", done=True, error={"code": 8}, response=None)]
    with pytest.raises(SandboxUnavailableError):
        await provider.create(600, "c")


async def test_create_that_never_finishes_hits_the_deadline(provider, sdk, monkeypatch):
    monkeypatch.setattr(sandbox_module, "CREATE_POLL_S", 0.01)
    monkeypatch.setattr(sandbox_module, "CREATE_TIMEOUT_S", 0.05)
    sdk.operation_polls = [SimpleNamespace(name="operations/1", done=False, error=None, response=None)]
    with pytest.raises(SandboxUnavailableError, match="took over"):
        await provider.create(600, "c")


def test_real_client_is_pinned_to_httpx(monkeypatch):
    import agentplatform

    captured = {}
    monkeypatch.setattr(agentplatform, "Client", lambda **kwargs: captured.update(kwargs) or object())
    AgentEngineProvider(ENGINE)

    assert captured["project"] == "123" and captured["location"] == "asia-south1"
    assert isinstance(captured["http_options"]["async_client_args"]["transport"], httpx.AsyncHTTPTransport)


async def test_missing_expire_time_is_bounded(provider, sdk):
    expires_at = await provider.expires_at(SANDBOX)
    assert time.time() < expires_at <= time.time() + sandbox_module.SANDBOX_TTL_S + 1


@pytest.mark.parametrize(
    "outputs",
    [
        [chunk(b"{not json", "application/json")],
        [chunk(b"[1, 2]", "application/json")],
        [chunk(b'{"exit_status_int": "oops"}', "application/json")],
    ],
)
def test_malformed_response_maps_to_unavailable(outputs):
    with pytest.raises(SandboxUnavailableError):
        parse_execute_response(SimpleNamespace(outputs=outputs))


# --- reaper vs active runs --------------------------------------------------------------------------------


async def test_claimed_sandbox_cannot_be_touched_and_vice_versa(async_redis_db):
    slots = SandboxSlots(async_redis_db, capacity=5)
    old = time.time() - 1000
    await slots.touch("a", old)

    assert await slots.claim_idle("a", time.time())
    assert not await slots.touch("a", time.time())  # the run lost: treat it as gone
    assert not await slots.claim_idle("a", time.time())  # exactly one claim
    assert await slots.idle_since(time.time(), 10) == []  # claimed members aren't candidates

    await slots.touch("b", old)
    assert await slots.touch("b", time.time() + 60)  # the run won: busy
    assert not await slots.claim_idle("b", time.time())


async def test_ensure_marks_a_reused_sandbox_busy_so_the_reaper_skips_it(async_redis_db):
    provider = FakeSandboxProvider()
    manager = make_manager(async_redis_db, provider)
    handle, _ = await manager.ensure(None, "c")
    await manager.slots.touch(handle.name, time.time() - 1000)  # long idle

    again, fresh = await manager.ensure(handle.name, "c")
    assert again.name == handle.name and not fresh
    assert await manager.reap_idle() == 0  # hydration may take a while; it stays busy meanwhile
    assert provider.deleted == []


async def test_ensure_starts_fresh_when_the_reaper_claimed_the_sandbox(async_redis_db):
    provider = FakeSandboxProvider()
    manager = make_manager(async_redis_db, provider)
    handle, _ = await manager.ensure(None, "c")
    await manager.slots.touch(handle.name, time.time() - 1000)
    assert await manager.slots.claim_idle(handle.name, time.time())

    replacement, fresh = await manager.ensure(handle.name, "c")

    assert fresh and replacement.name != handle.name


async def test_execute_on_a_claimed_sandbox_is_gone_and_keeps_the_claim(async_redis_db):
    provider = FakeSandboxProvider()
    manager = make_manager(async_redis_db, provider)
    handle, _ = await manager.ensure(None, "c")
    await manager.slots.touch(handle.name, time.time() - 1000)
    assert await manager.slots.claim_idle(handle.name, time.time())

    with pytest.raises(SandboxGone):
        await manager.execute(handle, "x")

    assert provider.created[0].executed == []
    assert await async_redis_db.zscore(LAST_USED_KEY, handle.name) == sandbox_module.CLAIMED
    assert await manager.slots.expires_at(handle.name) is not None  # the reaper releases it


async def test_execute_busy_mark_covers_the_given_deadline(async_redis_db):
    provider = FakeSandboxProvider()
    manager = make_manager(async_redis_db, provider)
    handle, _ = await manager.ensure(None, "c")
    seen = {}

    async def spy(sandbox, code):
        seen["busy_until"] = await async_redis_db.zscore(LAST_USED_KEY, handle.name)
        return ExecResult(0)

    provider.handler = spy
    start = time.time()
    await manager.execute(handle, "x", timeout_s=250)
    assert seen["busy_until"] >= start + 250


def test_delete_sandbox_sync_failure_keeps_redis_state(redis_db, monkeypatch):
    class BrokenClient:
        sandboxes = SimpleNamespace(delete=raiser(api_error(500, "INTERNAL")))

    monkeypatch.setattr(sandbox_module, "AGENT_ENGINE_NAME", ENGINE)
    monkeypatch.setattr(sandbox_module, "_sync_client", lambda: BrokenClient())
    redis_db.zadd(LEASES_KEY, {"sbx": time.time() + 60})

    with pytest.raises(SandboxUnavailableError):
        delete_sandbox_sync("sbx", redis_db)
    assert redis_db.zcard(LEASES_KEY) == 1  # still counted until it really goes
