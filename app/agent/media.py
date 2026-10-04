"""Media the model itself returns (images, audio, video, files in its message or a tool result) becomes an
artifact the user can view and download, like a file the sandbox code wrote (plan WP8A).

The declared MIME type is not trusted: the bytes must match a known signature, and only that allow-list is
saved. URL blocks are never fetched by the worker (SSRF) and are dropped. SVG/HTML are not on the list.
"""

import base64
import binascii
import hashlib
import logging
import uuid
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

logger = logging.getLogger("app.agent.media")

MAX_MEDIA_PER_RUN = 10
_MEDIA_TYPES = ("image", "audio", "video", "file")


def _sniff(data: bytes) -> tuple[str, str] | None:
    """(mime type, extension) from the leading bytes, or None if unknown."""
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", "webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav", "wav"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "audio/mpeg", "mp3"
    if head.startswith(b"OggS"):
        return "audio/ogg", "ogg"
    if head[4:8] == b"ftyp":
        return "video/mp4", "mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm", "webm"
    if head.startswith(b"%PDF-"):
        return "application/pdf", "pdf"
    return None


@dataclass(frozen=True)
class MediaBlock:
    artifact_id: uuid.UUID  # derived from the run and the bytes, so saving the same media again is a no-op
    filename: str
    content_type: str
    data: bytes


def _name(block: dict, extension: str, number: int) -> str:
    given = block.get("filename") or (block.get("extras") or {}).get("filename") or block.get("name")
    if isinstance(given, str) and given.strip():
        return given.strip()
    return f"media-{number}.{extension}"


def extract_media(run_id: uuid.UUID, message: BaseMessage, max_bytes: int) -> tuple[list[MediaBlock], list[str]]:
    """The saveable media of an AI or tool message, and a note for each block that was skipped."""
    if not isinstance(message, (AIMessage, ToolMessage)) or isinstance(message.content, str):
        return [], []
    found: list[MediaBlock] = []
    notes: list[str] = []
    for block in message.content_blocks:
        if block.get("type") not in _MEDIA_TYPES:
            continue
        if not block.get("base64"):
            notes.append("A media block given only as a URL was not saved.")
            continue
        try:
            data = base64.b64decode(block["base64"], validate=False)
        except (binascii.Error, ValueError):
            notes.append("A media block with invalid data was not saved.")
            continue
        if len(data) > max_bytes:
            notes.append(f"A media block was not saved: larger than {max_bytes} bytes.")
            continue
        sniffed = _sniff(data)
        if sniffed is None:
            notes.append("A media block of an unsupported type was not saved.")
            continue
        content_type, extension = sniffed
        artifact_id = uuid.uuid5(run_id, hashlib.sha256(data).hexdigest())
        if any(m.artifact_id == artifact_id for m in found):
            continue
        found.append(MediaBlock(artifact_id, _name(block, extension, len(found) + 1), content_type, data))
    return found, notes
