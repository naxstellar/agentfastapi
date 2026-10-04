import ast
import asyncio
import io
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from google.api_core.exceptions import NotFound
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.services.storage import SignedUrl


class FakeStorage:
    """In-memory stand-in for GcsStorage that enforces signed uploads the way GCS does."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self._signed_puts: dict[str, tuple[str, int]] = {}

    def upload_url(self, key: str, content_type: str, max_bytes: int) -> SignedUrl:
        self._signed_puts[key] = (content_type, max_bytes)
        headers = {"Content-Type": content_type, "x-goog-content-length-range": f"0,{max_bytes}"}
        return SignedUrl(f"https://fake-gcs.test/{key}?X-Goog-Signature=x", self._expiry(), headers)

    def put(self, key: str, data: bytes, content_type: str) -> int:
        """Simulates the browser's PUT to the signed URL; returns the HTTP status GCS would."""
        if key not in self._signed_puts:
            return 403
        signed_type, max_bytes = self._signed_puts[key]
        if content_type != signed_type:
            return 403
        if len(data) > max_bytes:
            return 400
        self.objects[key] = data
        return 200

    def download_url(self, key: str, filename: str) -> SignedUrl:
        return SignedUrl(f"https://fake-gcs.test/{key}?download={filename}", self._expiry(), {})

    def object_size(self, key: str) -> int | None:
        data = self.objects.get(key)
        return len(data) if data is not None else None

    def move(self, src_key: str, dst_key: str) -> None:
        self.objects[dst_key] = self.objects.pop(src_key)

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def upload(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data
        self.content_types[key] = content_type

    def open_read(self, key: str):
        if key not in self.objects:
            raise NotFound(f"No such object: {key}")
        return io.BytesIO(self.objects[key])

    @staticmethod
    def _expiry() -> datetime:
        return datetime.now(timezone.utc) + timedelta(minutes=15)


class FakeSandbox:
    def __init__(self, name: str, expires_at: float, display_name: str):
        self.name = name
        self.expires_at = expires_at
        self.display_name = display_name
        self.files: dict[str, bytes] = {}
        self.state: dict = {}  # stands in for Python variables
        self.executed: list[str] = []
        self.received: list[str] = []  # names of input files, in order


class FakeSandboxProvider:
    """In-memory Agent Engine: files and state persist, a timed-out or cancelled execution is rolled back,
    and concurrent executions in one sandbox are rejected (the real service runs both and rejects the
    later finisher; either way the loser leaves no trace). User code isn't run: `handler(sandbox, code)`
    decides the result. Housekeeping code is interpreted (its `_remove` list is applied).
    """

    def __init__(self):
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.created: list[FakeSandbox] = []
        self.deleted: list[str] = []
        self.running: set[str] = set()
        self.max_parallel = 0
        self.handler = self.default_handler
        self.create_delay = 0.0

    @staticmethod
    async def default_handler(sandbox: FakeSandbox, code: str):
        from app.agent.sandbox import ExecResult

        return ExecResult(exit_status=0, stdout=f"ran: {code}\n")

    async def create(self, ttl_s: int, display_name: str):
        from app.agent.sandbox import SandboxHandle

        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        sandbox = FakeSandbox(f"sbx-{len(self.created) + 1}", time.time() + ttl_s, display_name)
        self.sandboxes[sandbox.name] = sandbox
        self.created.append(sandbox)
        return SandboxHandle(sandbox.name, sandbox.expires_at)

    async def expires_at(self, name: str) -> float | None:
        sandbox = self.sandboxes.get(name)
        return sandbox.expires_at if sandbox is not None else None

    async def execute(self, name, code, files, timeout_s):
        from app.agent.sandbox import ConcurrentExecutionError, ExecResult, ExecTimeout, SandboxGone
        from app.agent.workspace import HOUSEKEEPING_MARKER

        sandbox = self.sandboxes.get(name)
        if sandbox is None:
            raise SandboxGone
        if name in self.running:
            self.max_parallel = max(self.max_parallel, 2)  # counted even though it is rejected
            raise ConcurrentExecutionError
        self.running.add(name)
        self.max_parallel = max(self.max_parallel, len(self.running))
        snapshot = (dict(sandbox.files), dict(sandbox.state))
        try:
            for f in files:
                sandbox.files[f.name] = f.data
                sandbox.received.append(f.name)
            sandbox.executed.append(code)
            if code.startswith(HOUSEKEEPING_MARKER):
                line = next(l for l in code.splitlines() if l.startswith("_remove = "))
                for path in ast.literal_eval(line.removeprefix("_remove = ")):
                    sandbox.files.pop(path, None)
                return ExecResult(exit_status=0)
            async with asyncio.timeout(timeout_s):
                return await self.handler(sandbox, code)
        except TimeoutError:
            sandbox.files, sandbox.state = snapshot
            raise ExecTimeout
        except asyncio.CancelledError:
            sandbox.files, sandbox.state = snapshot
            raise
        finally:
            self.running.discard(name)

    async def delete(self, name: str) -> None:
        self.deleted.append(name)
        self.sandboxes.pop(name, None)

    def expire(self, name: str) -> None:
        """Simulates the service deleting the sandbox when its TTL runs out."""
        self.sandboxes.pop(name, None)


class ScriptedChatModel(BaseChatModel):
    """A chat model whose reply is `responder(messages)`. Records every call's input messages.

    bind_tools() returns the model itself, so the agent can "call tools" through scripted AIMessages.
    """

    responder: Callable[[list[BaseMessage]], AIMessage]
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.responder(list(messages)))])
