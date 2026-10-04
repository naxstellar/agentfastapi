"""GCS access: V4 signed upload/download URLs, object metadata, move, delete and streamed reads.

Signing goes through IAM signBlob as GCS_SIGNER_SERVICE_ACCOUNT, so no key files are needed: laptops
use ADC impersonating the `local-dev` service account, GKE pods use their Workload Identity account.
"""

import re
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import PurePosixPath
from typing import BinaryIO, Protocol
from urllib.parse import quote

import google.auth
import google.auth.transport.requests
from google.api_core.exceptions import NotFound
from google.cloud import storage

from app.config import GCS_BUCKET, GCS_SIGNER_SERVICE_ACCOUNT

UPLOAD_URL_TTL = timedelta(minutes=15)
DOWNLOAD_URL_TTL = timedelta(minutes=5)

PENDING_PREFIX = "pending/"
_MAX_FILENAME_CHARS = 100


@dataclass(frozen=True)
class SignedUrl:
    url: str
    expires_at: datetime
    headers: dict[str, str]


class Storage(Protocol):
    def upload_url(self, key: str, content_type: str, max_bytes: int) -> SignedUrl: ...
    def download_url(self, key: str, filename: str) -> SignedUrl: ...
    def object_size(self, key: str) -> int | None: ...
    def move(self, src_key: str, dst_key: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def open_read(self, key: str) -> BinaryIO: ...
    def upload(self, key: str, data: bytes, content_type: str) -> None: ...


def _ascii_slug(text: str, allowed: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(f"[^{allowed}]+", "_", text).strip("._-")


def safe_filename(filename: str) -> str:
    """A storage-safe name derived from a user-supplied filename (never used raw in object keys)."""
    name = PurePosixPath(filename.replace("\\", "/")).name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    ext = _ascii_slug(ext, "A-Za-z0-9").lower()[:10]
    stem = _ascii_slug(stem, "A-Za-z0-9._-")[: _MAX_FILENAME_CHARS - len(ext) - 1].rstrip("._-") or "file"
    return f"{stem}.{ext}" if ext else stem


def dataset_key(user_id: uuid.UUID, dataset_id: uuid.UUID, filename: str) -> str:
    return f"users/{user_id}/datasets/{dataset_id}/{safe_filename(filename)}"


def pending_dataset_key(user_id: uuid.UUID, dataset_id: uuid.UUID, filename: str) -> str:
    # Uploads land under pending/ (a lifecycle rule deletes abandoned ones) and move on /complete.
    return PENDING_PREFIX + dataset_key(user_id, dataset_id, filename)


def artifact_key(user_id: uuid.UUID, run_id: uuid.UUID, artifact_id: uuid.UUID, filename: str) -> str:
    # artifact_id keeps keys unique when one run produces the same filename twice.
    return f"users/{user_id}/artifacts/{run_id}/{artifact_id}/{safe_filename(filename)}"


def _expiry(ttl: timedelta) -> datetime:
    return datetime.now(timezone.utc) + ttl


class GcsStorage:
    def __init__(self, bucket_name: str, signer_email: str | None):
        self._credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        # project=None: object operations don't need one, and impersonated ADC doesn't carry one.
        self._client = storage.Client(project=None, credentials=self._credentials)
        self._bucket = self._client.bucket(bucket_name)
        self._signer_email = signer_email
        self._refresh_lock = threading.Lock()

    def _signing_kwargs(self) -> dict:
        if not self._signer_email:
            return {}  # credentials that hold a private key can sign locally
        with self._refresh_lock:
            if not self._credentials.valid:
                self._credentials.refresh(google.auth.transport.requests.Request())
            token = self._credentials.token
        return {"service_account_email": self._signer_email, "access_token": token}

    def upload_url(self, key: str, content_type: str, max_bytes: int) -> SignedUrl:
        # Both headers are part of the signature, so GCS itself rejects a different type or a bigger body.
        headers = {"Content-Type": content_type, "x-goog-content-length-range": f"0,{max_bytes}"}
        url = self._bucket.blob(key).generate_signed_url(
            version="v4",
            expiration=UPLOAD_URL_TTL,
            method="PUT",
            headers=headers,
            **self._signing_kwargs(),
        )
        return SignedUrl(url=url, expires_at=_expiry(UPLOAD_URL_TTL), headers=headers)

    def download_url(self, key: str, filename: str) -> SignedUrl:
        disposition = f"attachment; filename=\"{safe_filename(filename)}\"; filename*=UTF-8''{quote(filename)}"
        url = self._bucket.blob(key).generate_signed_url(
            version="v4",
            expiration=DOWNLOAD_URL_TTL,
            method="GET",
            response_disposition=disposition,
            **self._signing_kwargs(),
        )
        return SignedUrl(url=url, expires_at=_expiry(DOWNLOAD_URL_TTL), headers={})

    def object_size(self, key: str) -> int | None:
        blob = self._bucket.get_blob(key)
        return blob.size if blob is not None else None

    def move(self, src_key: str, dst_key: str) -> None:
        self._bucket.rename_blob(self._bucket.blob(src_key), dst_key)

    def delete(self, key: str) -> None:
        try:
            self._bucket.blob(key).delete()
        except NotFound:
            pass

    def open_read(self, key: str) -> BinaryIO:
        return self._bucket.blob(key).open("rb")

    def upload(self, key: str, data: bytes, content_type: str) -> None:
        self._bucket.blob(key).upload_from_string(data, content_type=content_type)


class StorageNotConfiguredError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    if not GCS_BUCKET:
        raise StorageNotConfiguredError("GCS_BUCKET is not set; see CLAUDE.md 'GCS setup'")
    return GcsStorage(GCS_BUCKET, GCS_SIGNER_SERVICE_ACCOUNT)
