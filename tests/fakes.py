import io
from datetime import datetime, timedelta, timezone

from google.api_core.exceptions import NotFound

from app.services.storage import SignedUrl


class FakeStorage:
    """In-memory stand-in for GcsStorage that enforces signed uploads the way GCS does."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
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

    def open_read(self, key: str):
        if key not in self.objects:
            raise NotFound(f"No such object: {key}")
        return io.BytesIO(self.objects[key])

    @staticmethod
    def _expiry() -> datetime:
        return datetime.now(timezone.utc) + timedelta(minutes=15)
