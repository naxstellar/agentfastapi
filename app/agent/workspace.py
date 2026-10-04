"""A run's view of its conversation's sandbox: dataset hydration, code execution and artifact extraction.

The sandbox never gets URLs or credentials. The worker streams each attached dataset from GCS and sends
it inside an execute request, and turns files the code writes under OUTPUTS_DIR (plus figures shown
with plt.show()) into artifacts.
"""

import asyncio
import logging
import mimetypes
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

import google.auth.exceptions
import requests
from google.api_core.exceptions import GoogleAPIError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agent.sandbox import (
    ExecResult,
    ExecTimeout,
    InputFile,
    OutputFile,
    SandboxBusyError,
    SandboxGone,
    SandboxHandle,
    SandboxManager,
    SandboxUnavailableError,
    datasets_key,
)
from app.config import MAX_ARTIFACT_BYTES, MAX_UPLOAD_BYTES
from app.db import SessionLocal
from app.models_agent import Artifact, Conversation, ConversationDataset, Dataset
from app.services.storage import Storage, artifact_key, safe_filename

logger = logging.getLogger("app.agent.workspace")

# Relative to the sandbox's working directory, which is stable across executions (unless code chdirs).
SANDBOX_HOME = "/home/bard"
DATA_DIR = "data"
OUTPUTS_DIR = "outputs"
HOUSEKEEPING_MARKER = "# sandbox housekeeping"
# Empty directories (and empty files) don't persist between executions, so this placeholder is sent
# with every execution: input files are written before the code runs, so outputs/ always exists.
OUTPUTS_PLACEHOLDER = f"{OUTPUTS_DIR}/.keep"

MAX_OUTPUT_CHARS = 10_000  # per stream (stdout, stderr) returned to the LLM
MAX_ARTIFACTS_PER_EXEC = 10
# Raw bytes of datasets per execute request; base64 inflates this by 4/3, under the 100 MB request cap.
HYDRATE_BATCH_BYTES = 50 * 1024 * 1024
MAX_ATTEMPTS = 3  # sandbox lost mid-turn -> new sandbox, rehydrate, run again
# GCS failures. A dataset that can't be read is skipped (and retried on the next call), and an artifact
# that can't be saved is reported; neither fails the call.
STORAGE_ERRORS = (GoogleAPIError, google.auth.exceptions.GoogleAuthError, requests.RequestException)
DATASET_LOAD_ERRORS = (ValueError, *STORAGE_ERRORS)  # ValueError: larger than the upload limit
_CHUNK = 1024 * 1024
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# The service's own exec() wrapper frame: a "File" line plus its indented source/caret lines.
_ENTRY_FRAME = re.compile(r'^  File "/usr/bin/entry/entry_point".*\n(?:    .*\n)*', re.MULTILINE)
_FIGURE = re.compile(r"output_[^/]*\.png")
# Built-in table only: on Windows the module-level functions also read the registry (.csv -> vnd.ms-excel).
_MIME_TYPES = mimetypes.MimeTypes()
_MIME_TYPES.add_type("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx")
_MIME_TYPES.add_type("application/vnd.apache.parquet", ".parquet")

RESET_NOTE = (
    "Note: this ran in a new sandbox session (the previous one expired or was recycled), so earlier "
    f"variables, imports and files are gone. Attached datasets, if any, are in {DATA_DIR}/ again; "
    "reload what you need."
)

EmitFn = Callable[[str, dict], Awaitable[None]]
SessionFactory = Callable[[], AbstractContextManager[Session]]


@dataclass(frozen=True)
class SandboxDataset:
    id: uuid.UUID
    filename: str
    gcs_key: str
    size_bytes: int
    path: str  # where it lives inside the sandbox, e.g. "data/sales.xlsx"
    # From the dataset inspection: [{"name", "columns", "row_count", "rows_truncated"}, ...]
    sheets: list[dict] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ArtifactInfo:
    id: uuid.UUID
    kind: str  # "image" | "file"
    filename: str


@dataclass
class RunPythonResult:
    output: str  # LLM-facing, truncated
    is_error: bool
    artifacts: list[ArtifactInfo] = field(default_factory=list)


class ConversationGoneError(Exception):
    """The conversation was deleted (or changed owner) while the run was using it."""


def sandbox_paths(datasets: list[tuple[uuid.UUID, str]]) -> dict[uuid.UUID, str]:
    """Sandbox path for each (dataset_id, filename); colliding names get a dataset-id suffix."""
    names = {dataset_id: safe_filename(filename) for dataset_id, filename in datasets}
    counts = Counter(names.values())
    paths = {}
    for dataset_id, name in names.items():
        if counts[name] > 1:
            stem, dot, ext = name.rpartition(".")
            suffix = dataset_id.hex[:8]
            name = f"{stem}_{suffix}.{ext}" if dot else f"{name}_{suffix}"
        paths[dataset_id] = f"{DATA_DIR}/{name}"
    return paths


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit * 7 // 10
    tail = limit - head
    return f"{text[:head]}\n… [{len(text) - limit} characters truncated] …\n{text[-tail:]}"


def clean_stderr(text: str) -> str:
    return _ENTRY_FRAME.sub("", _ANSI.sub("", text))


def is_artifact_path(name: str) -> bool:
    """Files under outputs/ and the figures plt.show() saves; everything else is the agent's scratch."""
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts) or name == OUTPUTS_PLACEHOLDER:
        return False
    return (parts[0] == OUTPUTS_DIR and len(parts) > 1) or (len(parts) == 1 and bool(_FIGURE.fullmatch(name)))


def housekeeping_code(remove: list[str]) -> str:
    # Paths come from sandbox_paths() (safe names), never from user input; repr() makes them literals.
    # Absolute, so an os.chdir() in earlier agent code can't make removals miss.
    # Datasets themselves travel as input files, which the service writes before the code runs.
    return (
        f"{HOUSEKEEPING_MARKER}\n"
        "import os\n"
        f"_remove = {remove!r}\n"
        "for _p in _remove:\n"
        "    try:\n"
        f"        os.remove(os.path.join({SANDBOX_HOME!r}, _p))\n"
        "    except FileNotFoundError:\n"
        "        pass\n"
        "del _remove\n"
    )


async def _noop_emit(event_type: str, payload: dict) -> None:
    pass


class ConversationWorkspace:
    """One per run. Executions are serialized: the agent may issue parallel tool calls, and the service
    rejects a second execution while one is running in the same sandbox.
    """

    def __init__(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        run_id: uuid.UUID,
        manager: SandboxManager,
        storage: Storage,
        session_factory: SessionFactory = SessionLocal,
        emit: EmitFn = _noop_emit,
    ):
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.run_id = run_id
        self._manager = manager
        self._storage = storage
        self._session_factory = session_factory
        self._emit = emit
        self._lock = asyncio.Lock()

    async def run_python(self, code: str) -> RunPythonResult:
        async with self._lock:
            return await self._run_locked(code)

    async def _run_locked(self, code: str) -> RunPythonResult:
        reset = False
        created: set[str] = set()  # sandboxes made during this call hold no earlier state
        try:
            for attempt in range(MAX_ATTEMPTS):
                handle, replaced, load_notes = await self._prepare(created)
                reset = reset or replaced
                try:
                    result = await self._manager.execute(handle, code, [InputFile(OUTPUTS_PLACEHOLDER, b"")])
                    break
                except SandboxGone:
                    # Nothing ran (or it ran in a sandbox that no longer exists); start over in a new one.
                    if attempt == MAX_ATTEMPTS - 1:
                        raise SandboxUnavailableError("sandbox kept disappearing")
        except ExecTimeout:
            return RunPythonResult(
                f"Error: execution exceeded the {self._manager.exec_timeout_s} s time limit and was rolled back; "
                "variables and files are as before.",
                is_error=True,
            )
        except SandboxBusyError:
            return RunPythonResult("Error: no code sandbox is available right now; try again later.", is_error=True)
        except SandboxUnavailableError as exc:
            logger.warning("Sandbox unavailable for conversation %s: %s", self.conversation_id, exc)
            return RunPythonResult("Error: code execution is temporarily unavailable; try again later.", is_error=True)

        artifacts, notes = await self._collect_artifacts(result.files)
        return RunPythonResult(
            self._format(result, reset, load_notes, artifacts, notes),
            is_error=result.exit_status != 0,
            artifacts=artifacts,
        )

    async def _status(self, message: str) -> None:
        await self._emit("status", {"message": message})

    async def _prepare(self, created: set[str]) -> tuple[SandboxHandle, bool, list[str]]:
        """A sandbox holding the conversation's datasets.

        Returns (handle, replaced_a_previous_one, notes about datasets that couldn't be loaded).
        """
        replaced = False
        for _ in range(MAX_ATTEMPTS):
            previous = await asyncio.to_thread(self._load_sandbox_id)
            display_name = f"conversation-{self.conversation_id}"
            handle, fresh = await self._manager.ensure(previous, display_name, on_status=self._status)
            if handle.name != previous and not await asyncio.to_thread(self._save_sandbox_id, handle.name, previous):
                try:
                    await self._manager.delete(handle.name)
                except SandboxUnavailableError:
                    logger.warning("Could not delete sandbox %s of a deleted conversation", handle.name)
                raise ConversationGoneError(str(self.conversation_id))
            if fresh:
                created.add(handle.name)
            # Only losing a sandbox from an earlier call loses the agent's state.
            replaced = replaced or (fresh and previous is not None and previous not in created)
            try:
                notes = await self._hydrate(handle, fresh)
            except SandboxGone:
                # A reused sandbox's state is lost; a brand-new one held nothing yet.
                replaced = replaced or handle.name not in created
                continue
            return handle, replaced, notes
        raise SandboxUnavailableError("sandbox kept disappearing during hydration")

    # --- datasets -------------------------------------------------------------------------------------

    async def _hydrate(self, handle: SandboxHandle, fresh: bool) -> list[str]:
        """Sends attached datasets the sandbox doesn't have yet and removes detached ones.

        Files persist in the sandbox, so Redis remembers what was sent (dataset id -> path) and a dataset
        is sent again only to a new sandbox or when its path changes. Returns a note per dataset that
        couldn't be read from storage or whose upload timed out; those aren't recorded as sent, so the
        next call retries them.
        """
        datasets = await asyncio.to_thread(self.load_datasets)
        key = datasets_key(handle.name)
        sent: dict[str, str] = {} if fresh else await self._manager.redis.hgetall(key)
        wanted = {str(d.id): d for d in datasets}
        # Input files are written before the code runs, so never remove a path that is being (re)sent.
        remove = sorted(set(sent.values()) - {d.path for d in datasets})
        send = [d for dataset_id, d in wanted.items() if sent.get(dataset_id) != d.path]
        stale = [dataset_id for dataset_id in sent if dataset_id not in wanted]
        if not (remove or send):
            return []
        if send:
            await self._status(f"Loading {len(send)} dataset(s) into the sandbox…")

        notes = []
        for index, batch in enumerate(self._batches(send) or [[]]):
            files, loaded = [], []
            for dataset in batch:
                try:
                    data = await asyncio.to_thread(self._download, dataset.gcs_key)
                except DATASET_LOAD_ERRORS as exc:
                    # Type only: storage error messages contain object keys.
                    logger.warning("Could not load dataset %s: %s", dataset.id, type(exc).__name__)
                    notes.append(
                        f"Note: dataset {dataset.path} could not be loaded into the sandbox (storage error), "
                        "so it is missing for this call; it will be retried on the next call."
                    )
                    continue
                files.append(InputFile(dataset.path, data))
                loaded.append(dataset)
            if not files and index > 0:
                continue
            try:
                result = await self._manager.execute(
                    handle,
                    housekeeping_code(remove if index == 0 else []),
                    files,
                    timeout_s=self._manager.housekeeping_timeout_s,
                )
            except ExecTimeout:
                # Rolled back like any execution: nothing was written or removed, so nothing is recorded.
                logger.warning("Dataset upload timed out for sandbox %s", handle.name)
                notes.extend(
                    f"Note: dataset {d.path} could not be loaded into the sandbox (upload timed out), "
                    "so it is missing for this call; it will be retried on the next call."
                    for d in loaded
                )
                continue
            if result.exit_status != 0:
                raise SandboxUnavailableError(f"housekeeping failed with exit status {result.exit_status}")
            async with self._manager.redis.pipeline(transaction=True) as pipe:
                if index == 0 and stale:
                    pipe.hdel(key, *stale)
                if loaded:
                    pipe.hset(key, mapping={str(d.id): d.path for d in loaded})
                pipe.expireat(key, int(handle.expires_at) + 60)
                await pipe.execute()
        return notes

    @staticmethod
    def _batches(datasets: list[SandboxDataset]) -> list[list[SandboxDataset]]:
        batches: list[list[SandboxDataset]] = []
        size = 0
        for dataset in datasets:
            if batches and size + dataset.size_bytes <= HYDRATE_BATCH_BYTES:
                batches[-1].append(dataset)
                size += dataset.size_bytes
            else:
                batches.append([dataset])
                size = dataset.size_bytes
        return batches

    def _download(self, key: str) -> bytes:
        data = bytearray()
        with self._storage.open_read(key) as src:
            while chunk := src.read(_CHUNK):
                data += chunk
                if len(data) > MAX_UPLOAD_BYTES:
                    raise ValueError("Dataset object is larger than the upload limit")
        return bytes(data)

    def load_datasets(self) -> list[SandboxDataset]:
        """The conversation's attached, ready datasets owned by the user, with their sandbox paths."""
        with self._session_factory() as db:
            rows = db.execute(
                select(Dataset.id, Dataset.filename, Dataset.gcs_key, Dataset.size_bytes, Dataset.sheets)
                .join(ConversationDataset, ConversationDataset.dataset_id == Dataset.id)
                .where(
                    ConversationDataset.conversation_id == self.conversation_id,
                    Dataset.user_id == self.user_id,
                    Dataset.status == "ready",
                )
                .order_by(Dataset.created_at, Dataset.id)
            ).all()
        paths = sandbox_paths([(row.id, row.filename) for row in rows])
        return [
            SandboxDataset(row.id, row.filename, row.gcs_key, row.size_bytes, paths[row.id], row.sheets)
            for row in rows
        ]

    # --- conversation.sandbox_id ----------------------------------------------------------------------

    def _load_sandbox_id(self) -> str | None:
        with self._session_factory() as db:
            return db.scalar(
                select(Conversation.sandbox_id).where(
                    Conversation.id == self.conversation_id, Conversation.user_id == self.user_id
                )
            )

    def _save_sandbox_id(self, sandbox_id: str, expected: str | None) -> bool:
        with self._session_factory() as db:
            result = db.execute(
                update(Conversation)
                .where(
                    Conversation.id == self.conversation_id,
                    Conversation.user_id == self.user_id,
                    Conversation.sandbox_id.is_(None) if expected is None else Conversation.sandbox_id == expected,
                )
                # Keep updated_at: a sandbox swap isn't conversation activity.
                .values(sandbox_id=sandbox_id, updated_at=Conversation.updated_at)
            )
            db.commit()
            return result.rowcount == 1

    # --- artifacts ------------------------------------------------------------------------------------

    async def _collect_artifacts(self, files: list[OutputFile]) -> tuple[list[ArtifactInfo], list[str]]:
        artifacts: list[ArtifactInfo] = []
        notes: list[str] = []
        candidates = [f for f in files if is_artifact_path(f.name)]
        figures = 0
        for index, file in enumerate(candidates):
            if len(artifacts) >= MAX_ARTIFACTS_PER_EXEC:
                dropped = ", ".join(f.name for f in candidates[index:])
                notes.append(f"Not saved (limit {MAX_ARTIFACTS_PER_EXEC} files per execution): {dropped}")
                break
            if len(file.data) > MAX_ARTIFACT_BYTES:
                notes.append(f"{file.name} was not saved: larger than {MAX_ARTIFACT_BYTES} bytes.")
                continue
            if file.name.startswith(f"{OUTPUTS_DIR}/"):
                filename = file.name.rsplit("/", 1)[1]
            else:
                figures += 1
                filename = f"figure-{figures}.png"
            try:
                artifacts.append(await self._save_artifact(filename, file.data))
            except STORAGE_ERRORS as exc:
                # The code already ran and committed; report the lost file instead of failing the call.
                logger.warning("Could not save an artifact of run %s: %s", self.run_id, type(exc).__name__)
                notes.append(f"{file.name} could not be saved for the user (storage error); try saving it again.")
        return artifacts, notes

    async def _save_artifact(self, filename: str, data: bytes) -> ArtifactInfo:
        artifact_id = uuid.uuid4()
        filename = safe_filename(filename)
        content_type = _MIME_TYPES.guess_type(filename)[0] or "application/octet-stream"
        kind = "image" if content_type.startswith("image/") else "file"
        key = artifact_key(self.user_id, self.run_id, artifact_id, filename)
        # Object first: an orphaned object is harmless, a row pointing at nothing is not.
        await asyncio.to_thread(self._storage.upload, key, data, content_type)
        await asyncio.to_thread(self._insert_artifact, artifact_id, kind, key, filename)
        await self._emit("artifact", {"artifact_id": str(artifact_id), "kind": kind, "filename": filename})
        return ArtifactInfo(artifact_id, kind, filename)

    def _insert_artifact(self, artifact_id: uuid.UUID, kind: str, key: str, filename: str) -> None:
        with self._session_factory() as db:
            db.add(
                Artifact(
                    id=artifact_id, run_id=self.run_id, user_id=self.user_id, kind=kind, gcs_key=key, filename=filename
                )
            )
            db.commit()

    # --- LLM-facing output ----------------------------------------------------------------------------

    @staticmethod
    def _format(
        result: ExecResult, reset: bool, load_notes: list[str], artifacts: list[ArtifactInfo], notes: list[str]
    ) -> str:
        parts = ([RESET_NOTE] if reset else []) + load_notes
        stdout = _ANSI.sub("", result.stdout).rstrip()
        stderr = clean_stderr(result.stderr).rstrip()
        if stdout:
            parts.append("stdout:\n" + truncate(stdout))
        if stderr:
            parts.append("stderr:\n" + truncate(stderr))
        if result.exit_status != 0 and not stderr:
            parts.append(f"error: exit status {result.exit_status}")
        if artifacts:
            parts.append("Saved for the user: " + ", ".join(a.filename for a in artifacts))
        parts.extend(notes)
        return "\n\n".join(parts) or "(no output)"
