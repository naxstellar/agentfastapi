"""Code-execution sandboxes behind a small async interface, implemented on Vertex AI Agent Engine.

One sandbox per conversation. Python state and files persist across executions, and an execution
that times out or is cancelled is rolled back by the service. A sandbox's TTL is fixed at creation
(executions don't extend it and there's no update API), so SANDBOX_TTL_S is its maximum lifetime and
idle expiry is ours: a Redis `last_used` set lets `reap_idle()` delete sandboxes unused for
SANDBOX_IDLE_TIMEOUT_S. A Redis lease set caps live sandboxes at MAX_CONCURRENT_SANDBOXES.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

import httpx
import redis
import redis.asyncio
from redis.exceptions import WatchError

from app.config import (
    AGENT_ENGINE_NAME,
    MAX_CONCURRENT_SANDBOXES,
    SANDBOX_EXEC_TIMEOUT_S,
    SANDBOX_IDLE_TIMEOUT_S,
    SANDBOX_MACHINE_CONFIG,
    SANDBOX_TTL_S,
)

logger = logging.getLogger("app.agent.sandbox")

LEASES_KEY = "sandboxes:leases"  # sandbox name -> expiry (epoch s)
LAST_USED_KEY = "sandboxes:last_used"  # sandbox name -> last activity (in the future while executing)
LEASE_MARGIN_S = 30
BUSY_WAIT_S = 120
SERVER_EXEC_CAP_S = 300  # the service ends longer executions with 503 UNAVAILABLE
CREATE_TIMEOUT_S = 300
CREATE_POLL_S = 1.0
# Deadline for housekeeping executions that carry datasets: the upload counts against it, and 50 MiB
# took ~45 s from a laptop. Below the ~300 s server cap.
HOUSEKEEPING_TIMEOUT_S = 280
CLAIMED = 0  # LAST_USED_KEY score of a sandbox the reaper is deleting
_ENGINE_NAME_RE = re.compile(r"projects/(?P<project>[^/]+)/locations/(?P<location>[^/]+)/reasoningEngines/[^/]+")

StatusFn = Callable[[str], Awaitable[None]]


def datasets_key(sandbox_name: str) -> str:
    """Redis hash of the datasets already sent into a sandbox: dataset id -> path inside it."""
    return f"sandbox:{sandbox_name}:datasets"


@dataclass(frozen=True)
class InputFile:
    name: str  # relative to the sandbox's working directory; may contain subdirectories
    data: bytes


@dataclass(frozen=True)
class OutputFile:
    name: str  # relative to the working directory, e.g. "outputs/summary.csv"
    data: bytes


@dataclass
class ExecResult:
    exit_status: int
    stdout: str = ""
    stderr: str = ""
    files: list[OutputFile] = field(default_factory=list)  # only files created or changed by this call


@dataclass(frozen=True)
class SandboxHandle:
    name: str
    expires_at: float  # epoch s


class SandboxError(Exception):
    pass


class SandboxGone(SandboxError):
    """The sandbox expired or was deleted; nothing ran."""


class SandboxReaped(SandboxGone):
    """The idle reaper claimed the sandbox and is deleting it; it also releases its Redis state."""


class ExecTimeout(SandboxError):
    """The execution hit its deadline and was rolled back: variables and files are as before."""


class ConcurrentExecutionError(SandboxError):
    """The service rejected the call because another execution is running in the same sandbox."""


class SandboxUnavailableError(SandboxError):
    """The service failed in a way that isn't the code's fault (5xx, network, malformed response)."""


class SandboxBusyError(SandboxError):
    """No sandbox slot became free within BUSY_WAIT_S."""


class SandboxNotConfiguredError(SandboxError):
    pass


class SandboxProvider(Protocol):
    async def create(self, ttl_s: int, display_name: str) -> SandboxHandle: ...
    async def expires_at(self, name: str) -> float | None: ...  # None if it's gone
    # Raises SandboxGone, ExecTimeout, ConcurrentExecutionError or SandboxUnavailableError.
    async def execute(self, name: str, code: str, files: list[InputFile], timeout_s: float) -> ExecResult: ...
    async def delete(self, name: str) -> None: ...  # idempotent


def _file_name(chunk) -> str | None:
    attrs = chunk.metadata.attributes if chunk.metadata and chunk.metadata.attributes else {}
    name = attrs.get("file_name")
    if isinstance(name, bytes):
        name = name.decode("utf-8", "replace")
    return name or None


def parse_execute_response(response) -> ExecResult:
    """One JSON chunk {exit_status_int, msg_out, msg_err} plus one chunk per created/changed file.

    The SDK's MIME types are ignored: it guesses them from the OS registry on Windows.
    """
    meta = None
    files = []
    try:
        for chunk in response.outputs or []:
            name = _file_name(chunk)
            if name is not None:
                files.append(OutputFile(name.removeprefix("/home/bard/").removeprefix("./"), chunk.data or b""))
            elif meta is None and chunk.mime_type == "application/json":
                meta = json.loads(chunk.data or b"{}")
        if not isinstance(meta, dict):
            raise SandboxUnavailableError("execute response has no result chunk")
        return ExecResult(
            exit_status=int(meta.get("exit_status_int") or 0),
            stdout=str(meta.get("msg_out") or ""),
            stderr=str(meta.get("msg_err") or ""),
            files=files,
        )
    except (ValueError, TypeError, AttributeError) as exc:  # JSONDecodeError is a ValueError
        raise SandboxUnavailableError(f"malformed execute response: {type(exc).__name__}") from exc


class AgentEngineProvider:
    """Create and execute go through the SDK's private async methods (`_create` + operation polling,
    `_execute_code`): only they can be cancelled and bounded by a deadline. Cancelling `_execute_code`
    aborts the request, and the service rolls the execution back. The public client is sync-only: its
    calls can't be cancelled from a thread, and its `create` polls the operation with no deadline.
    get/delete use the public client.
    """

    def __init__(self, engine_name: str, machine_config: str | None = None, client=None):
        match = _ENGINE_NAME_RE.fullmatch(engine_name)
        if match is None:
            raise SandboxNotConfiguredError("AGENT_ENGINE_NAME is malformed")
        if client is None:
            import agentplatform

            client = agentplatform.Client(
                project=match["project"],
                location=match["location"],
                # An explicit transport keeps the async client on httpx: genai silently switches to
                # aiohttp when it is importable, and the error mapping and the tested cancel/rollback
                # behaviour assume httpx.
                http_options={"async_client_args": {"transport": httpx.AsyncHTTPTransport()}},
            )
        self._client = client
        self._engine_name = engine_name
        environment = {"code_language": "LANGUAGE_PYTHON"}
        if machine_config:
            environment["machine_config"] = machine_config
        self._spec = {"code_execution_environment": environment}

    async def create(self, ttl_s: int, display_name: str) -> SandboxHandle:
        from agentplatform import types

        sandboxes = self._client.aio.sandboxes
        try:
            # The first sandbox on a new instance takes ~2 min (one-time setup), later ones ~1 s.
            async with asyncio.timeout(CREATE_TIMEOUT_S):
                operation = await sandboxes._create(
                    name=self._engine_name,
                    spec=self._spec,
                    config=types.CreateRuntimeSandboxConfig(display_name=display_name, ttl=f"{ttl_s}s"),
                )
                while not operation.done:
                    await asyncio.sleep(CREATE_POLL_S)
                    operation = await sandboxes._get_sandbox_operation(operation_name=operation.name)
                if operation.error or not operation.response or not operation.response.name:
                    raise SandboxUnavailableError("sandbox create failed")
                # The operation's response may lack fields such as expire_time.
                sandbox = await sandboxes._get(name=operation.response.name)
        except TimeoutError as exc:
            raise SandboxUnavailableError(f"sandbox create took over {CREATE_TIMEOUT_S} s") from exc
        except _service_errors() as exc:
            raise SandboxUnavailableError(f"create: {_describe(exc)}") from exc
        expires_at = sandbox.expire_time.timestamp() if sandbox.expire_time else time.time() + ttl_s
        return SandboxHandle(sandbox.name, expires_at)

    async def expires_at(self, name: str) -> float | None:
        from google.genai import errors

        try:
            sandbox = await asyncio.to_thread(self._client.sandboxes.get, name=name)
        except _service_errors() as exc:
            if isinstance(exc, errors.ClientError) and exc.code == 404:
                return None
            raise SandboxUnavailableError(f"get: {_describe(exc)}") from exc
        state = str(sandbox.state or "")
        if state.endswith(("DEPROVISIONING", "TERMINATED", "DELETED")):
            return None
        # We always create with a TTL; if the field is ever missing, assume the longest life it can have.
        return sandbox.expire_time.timestamp() if sandbox.expire_time else time.time() + SANDBOX_TTL_S

    async def execute(self, name: str, code: str, files: list[InputFile], timeout_s: float) -> ExecResult:
        import google.auth.exceptions
        from agentplatform import types
        from google.genai import errors

        inputs = [types.Chunk(mime_type="application/json", data=json.dumps({"code": code}).encode("utf-8"))]
        inputs += [
            # Empty MIME type, as the public execute_code sends for files (the tested path).
            types.Chunk(mime_type="", data=f.data, metadata={"attributes": {"file_name": f.name.encode("utf-8")}})
            for f in files
        ]
        # Server-side deadline as a backstop; the asyncio deadline below normally fires first.
        config = {"http_options": {"timeout": int((timeout_s + 5) * 1000)}}
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_s):
                response = await self._client.aio.sandboxes._execute_code(name=name, inputs=inputs, config=config)
        except TimeoutError as exc:
            raise ExecTimeout from exc
        except httpx.TimeoutException as exc:
            raise ExecTimeout from exc
        except (httpx.TransportError, google.auth.exceptions.GoogleAuthError) as exc:
            raise SandboxUnavailableError(type(exc).__name__) from exc
        except errors.APIError as exc:
            elapsed = time.monotonic() - started
            if exc.code == 504 or (exc.code == 503 and elapsed >= SERVER_EXEC_CAP_S - 30):
                raise ExecTimeout from exc  # the service's own deadline; never retried
            # An expired or deleted sandbox gives 400 FAILED_PRECONDITION, and so does a concurrent
            # execution; only `get` tells them apart. (If `get` fails too: SandboxUnavailableError.)
            if await self.expires_at(name) is None:
                raise SandboxGone from exc
            if exc.code == 400 and exc.status == "FAILED_PRECONDITION":
                raise ConcurrentExecutionError from exc
            raise SandboxUnavailableError(f"{exc.code} {exc.status}") from exc
        return parse_execute_response(response)

    async def delete(self, name: str) -> None:
        try:
            await asyncio.to_thread(_delete_quietly_sync, self._client, name)
        except _service_errors() as exc:
            raise SandboxUnavailableError(f"delete: {_describe(exc)}") from exc


def _service_errors() -> tuple[type[Exception], ...]:
    """Agent Engine failures that aren't our bug: API errors, network errors, credential refresh."""
    import google.auth.exceptions
    from google.genai import errors

    return errors.APIError, httpx.TransportError, google.auth.exceptions.GoogleAuthError


def _describe(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return f"{code} {getattr(exc, 'status', '')}".strip() if code else type(exc).__name__


def _delete_quietly_sync(client, name: str) -> None:
    from google.genai import errors

    try:
        client.sandboxes.delete(name=name)
    except errors.ClientError as exc:
        if exc.code != 404:
            raise


class SandboxSlots:
    """Counting semaphore for live sandboxes, as a Redis sorted set of leases scored by expiry time,
    plus a sorted set of last activity for idle reaping.

    Leases expire with their sandbox, so an expired sandbox frees its slot without any callback.
    WATCH/MULTI instead of Lua.
    """

    def __init__(self, client: redis.asyncio.Redis, capacity: int):
        self.redis = client
        self._capacity = capacity

    async def acquire(self, member: str, expires_at: float) -> bool:
        async with self.redis.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(LEASES_KEY)
                    now = time.time()
                    if await pipe.zcount(LEASES_KEY, now, "+inf") >= self._capacity:
                        await pipe.unwatch()
                        return False
                    pipe.multi()
                    pipe.zremrangebyscore(LEASES_KEY, "-inf", now)
                    pipe.zadd(LEASES_KEY, {member: expires_at + LEASE_MARGIN_S})
                    await pipe.execute()
                    return True
                except WatchError:
                    continue

    async def renew(self, member: str, expires_at: float, replaces: str | None = None) -> None:
        # No capacity check: the sandbox already exists, its slot must reflect that.
        async with self.redis.pipeline(transaction=True) as pipe:
            if replaces is not None:
                pipe.zrem(LEASES_KEY, replaces)
            pipe.zadd(LEASES_KEY, {member: expires_at + LEASE_MARGIN_S})
            await pipe.execute()

    async def expires_at(self, member: str) -> float | None:
        score = await self.redis.zscore(LEASES_KEY, member)
        return None if score is None else score - LEASE_MARGIN_S

    async def touch(self, member: str, when: float) -> bool:
        """Records activity (`when` in the future = busy until then). False if the reaper has claimed the
        sandbox, which then must be treated as gone. Atomic with claim_idle, so exactly one side wins.
        """
        async with self.redis.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(LAST_USED_KEY)
                    if await pipe.zscore(LAST_USED_KEY, member) == CLAIMED:
                        await pipe.unwatch()
                        return False
                    pipe.multi()
                    pipe.zadd(LAST_USED_KEY, {member: when})
                    await pipe.execute()
                    return True
                except WatchError:
                    continue

    async def idle_since(self, cutoff: float, limit: int) -> list[str]:
        return await self.redis.zrangebyscore(LAST_USED_KEY, f"({CLAIMED}", cutoff, start=0, num=limit)

    async def claim_idle(self, member: str, cutoff: float) -> bool:
        """Marks `member` as being reaped if it is still idle; True for exactly one caller."""
        async with self.redis.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(LAST_USED_KEY)
                    score = await pipe.zscore(LAST_USED_KEY, member)
                    if score is None or score == CLAIMED or score > cutoff:
                        await pipe.unwatch()
                        return False
                    pipe.multi()
                    pipe.zadd(LAST_USED_KEY, {member: CLAIMED})
                    await pipe.execute()
                    return True
                except WatchError:
                    continue

    async def release(self, member: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zrem(LEASES_KEY, member)
            pipe.zrem(LAST_USED_KEY, member)
            pipe.delete(datasets_key(member))
            await pipe.execute()


class SandboxManager:
    def __init__(
        self,
        provider: SandboxProvider,
        slots: SandboxSlots,
        *,
        ttl_s: int = SANDBOX_TTL_S,
        idle_timeout_s: int = SANDBOX_IDLE_TIMEOUT_S,
        exec_timeout_s: int = SANDBOX_EXEC_TIMEOUT_S,
        min_remaining_s: float | None = None,
        busy_wait_s: float = BUSY_WAIT_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.provider = provider
        self.slots = slots
        self.ttl_s = ttl_s
        self.idle_timeout_s = idle_timeout_s
        self.exec_timeout_s = exec_timeout_s
        self.housekeeping_timeout_s = max(HOUSEKEEPING_TIMEOUT_S, exec_timeout_s)
        # Enough lifetime left for a hydration and a full execution, so a sandbox is replaced between
        # turns instead of expiring in the middle of one.
        if min_remaining_s is None:
            min_remaining_s = exec_timeout_s + self.housekeeping_timeout_s + 60
        self.min_remaining_s = min_remaining_s
        self._busy_wait_s = busy_wait_s
        self._sleep = sleep
        self._background: set[asyncio.Task] = set()

    @property
    def redis(self) -> redis.asyncio.Redis:
        return self.slots.redis

    async def ensure(
        self, name: str | None, display_name: str, on_status: StatusFn | None = None
    ) -> tuple[SandboxHandle, bool]:
        """Reuses sandbox `name` if it has enough lifetime left, else creates one. Returns (handle, fresh)."""
        if name is not None:
            expires_at = await self.slots.expires_at(name)
            if expires_at is None:  # Redis lost track of it (flush, eviction): ask the service
                expires_at = await self.provider.expires_at(name)
                if expires_at is not None:
                    await self.slots.renew(name, expires_at)
            if expires_at is not None and expires_at - time.time() >= self.min_remaining_s:
                # Busy through hydration and the first execution, so the reaper leaves it alone.
                if await self.slots.touch(name, time.time() + self.min_remaining_s):
                    return SandboxHandle(name, expires_at), False
                # The reaper claimed it first and is deleting it (and releases it): start fresh.
            elif expires_at is not None and expires_at > time.time():
                # About to expire: recycle it now rather than mid-execution. A failed delete only means
                # it lingers until its (near) expiry, so it doesn't fail the turn.
                await self._delete_quietly(name)
                await self.slots.release(name)
            else:
                await self.slots.release(name)
        return await self._create(display_name, on_status), True

    async def _create(self, display_name: str, on_status: StatusFn | None) -> SandboxHandle:
        token = f"pending:{display_name}:{time.time_ns()}"
        await self._acquire(token, on_status)
        if on_status is not None:
            await on_status("Starting sandbox…")
        create = asyncio.ensure_future(self.provider.create(self.ttl_s, display_name))
        try:
            handle = await asyncio.shield(create)
        except asyncio.CancelledError:
            # The create keeps going in the background; delete what it makes so it doesn't leak.
            create.add_done_callback(self._delete_orphan)
            await asyncio.shield(self.slots.release(token))
            raise
        except BaseException:
            await asyncio.shield(self.slots.release(token))
            raise
        await self.slots.renew(handle.name, handle.expires_at, replaces=token)
        await self.slots.touch(handle.name, time.time() + self.min_remaining_s)
        logger.info("Created sandbox %s", handle.name)
        return handle

    def _delete_orphan(self, create: asyncio.Future) -> None:
        if create.cancelled() or create.exception() is not None:
            return
        task = asyncio.ensure_future(self._delete_quietly(create.result().name))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _acquire(self, token: str, on_status: StatusFn | None) -> None:
        deadline = time.monotonic() + self._busy_wait_s
        delay = 1.0
        notified = False
        expires_at = time.time() + self.ttl_s
        while not await self.slots.acquire(token, expires_at):
            if await self.reap_idle():
                continue
            if time.monotonic() + delay > deadline:
                raise SandboxBusyError("No sandbox capacity available")
            if on_status is not None and not notified:
                await on_status("Sandbox capacity is busy, retrying…")
                notified = True
            await self._sleep(delay)
            delay = min(delay * 2, 10.0)

    async def execute(
        self, handle: SandboxHandle, code: str, files: list[InputFile] = (), timeout_s: float | None = None
    ) -> ExecResult:
        """Runs `code` (deadline: exec_timeout_s unless given). Callers must serialize executions per
        sandbox (the service rejects concurrent ones).
        """
        timeout_s = self.exec_timeout_s if timeout_s is None else timeout_s
        files = list(files)
        try:
            try:
                return await self._execute_marked(handle, code, files, timeout_s)
            except ConcurrentExecutionError:
                # Shouldn't happen: executions are serialized, and one run at a time uses a conversation.
                logger.error("Concurrent execution rejected in sandbox %s; retrying once", handle.name)
                await self._sleep(1.0)
                try:
                    return await self._execute_marked(handle, code, files, timeout_s)
                except ConcurrentExecutionError as exc:
                    raise SandboxUnavailableError("sandbox is busy with another execution") from exc
        except SandboxReaped:
            raise  # keep the claim: releasing it would let ensure() re-adopt a sandbox being deleted
        except SandboxGone:
            await self.slots.release(handle.name)
            raise
        finally:
            if await self.slots.expires_at(handle.name) is not None:
                await self.slots.touch(handle.name, time.time())  # a no-op if the reaper claimed it

    async def _execute_marked(self, handle: SandboxHandle, code: str, files: list[InputFile], timeout_s: float):
        # Busy until the deadline, so the reaper never takes a sandbox that is executing.
        if not await self.slots.touch(handle.name, time.time() + timeout_s + LEASE_MARGIN_S):
            raise SandboxReaped
        return await self.provider.execute(handle.name, code, files, timeout_s)

    async def delete(self, name: str) -> None:
        try:
            await self.provider.delete(name)
        finally:
            await self.slots.release(name)

    async def reap_idle(self, limit: int = 20) -> int:
        """Deletes sandboxes unused for idle_timeout_s. Safe to run from many workers at once."""
        cutoff = time.time() - self.idle_timeout_s
        reaped = 0
        for name in await self.slots.idle_since(cutoff, limit):
            if not await self.slots.claim_idle(name, cutoff):
                continue
            try:
                expires_at = await self.slots.expires_at(name)
                if expires_at is not None and expires_at > time.time():
                    await self._delete_quietly(name)
                    reaped += 1
            finally:
                await asyncio.shield(self.slots.release(name))  # never leave a claim behind
        if reaped:
            logger.info("Reaped %d idle sandbox(es)", reaped)
        return reaped

    async def _delete_quietly(self, name: str) -> None:
        try:
            await self.provider.delete(name)
        except Exception as exc:
            logger.warning("Failed to delete sandbox %s: %s", name, type(exc).__name__)


def make_sandbox_manager(client: redis.asyncio.Redis) -> SandboxManager:
    """Manager for the current event loop; `client` must belong to that loop (worker: one per process)."""
    if not AGENT_ENGINE_NAME:
        raise SandboxNotConfiguredError("AGENT_ENGINE_NAME is not set")
    return SandboxManager(
        AgentEngineProvider(AGENT_ENGINE_NAME, SANDBOX_MACHINE_CONFIG), SandboxSlots(client, MAX_CONCURRENT_SANDBOXES)
    )


@lru_cache(maxsize=1)
def _sync_client():
    import agentplatform

    match = _ENGINE_NAME_RE.fullmatch(AGENT_ENGINE_NAME)
    return agentplatform.Client(project=match["project"], location=match["location"])


def delete_sandbox_sync(name: str, client: redis.Redis) -> None:
    """For sync API routes (e.g. conversation delete). Idempotent. Raises SandboxUnavailableError if the
    service fails.

    An execution already running finishes on its own (delete doesn't stop it), bounded by its timeout.
    """
    if AGENT_ENGINE_NAME:
        try:
            _delete_quietly_sync(_sync_client(), name)
        except _service_errors() as exc:
            # Redis state is kept: the lease still counts the live sandbox, and the reaper can retry.
            raise SandboxUnavailableError(f"delete: {_describe(exc)}") from exc
    with client.pipeline(transaction=True) as pipe:
        pipe.zrem(LEASES_KEY, name)
        pipe.zrem(LAST_USED_KEY, name)
        pipe.delete(datasets_key(name))
        pipe.execute()
