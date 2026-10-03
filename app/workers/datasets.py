import logging
import tempfile
import uuid
from pathlib import PurePosixPath

import google.auth.exceptions
import requests
from google.api_core.exceptions import NotFound, ServerError, TooManyRequests
from sqlalchemy import update
from sqlalchemy.orm import Session

import app.models  # noqa: F401 - registers users for the datasets FK
from app.config import MAX_UPLOAD_BYTES
from app.db import SessionLocal
from app.models_agent import Dataset
from app.redis_client import redis_client
from app.services.dataset_inspect import DatasetRejected, inspect_dataset
from app.services.storage import Storage, get_storage
from app.workers.celery_app import celery_app

logger = logging.getLogger("app.workers.datasets")

TRANSIENT_ERRORS = (
    ServerError,
    TooManyRequests,
    google.auth.exceptions.TransportError,
    requests.ConnectionError,
    requests.Timeout,
)
MAX_RETRIES = 3
# Long enough to cover retries with backoff; after it expires a stuck dataset can be re-enqueued.
ENQUEUE_DEDUPE_S = 15 * 60
_CHUNK = 1024 * 1024


def _finish(db: Session, dataset_id: uuid.UUID, **values) -> bool:
    # Conditional update: a no-op if the dataset was deleted or already processed meanwhile.
    result = db.execute(
        update(Dataset).where(Dataset.id == dataset_id, Dataset.status == "pending").values(**values)
    )
    db.commit()
    return result.rowcount == 1


def _reject(db: Session, storage: Storage, dataset_id: uuid.UUID, key: str, reason: str) -> None:
    if _finish(db, dataset_id, status="rejected", reject_reason=reason, sheets=None):
        storage.delete(key)
        logger.info("Rejected dataset id=%s: %s", dataset_id, reason)


def run_process_dataset(db: Session, storage: Storage, dataset_id: uuid.UUID) -> None:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.status != "pending":
        return
    key, filename = dataset.gcs_key, dataset.filename
    db.rollback()  # don't hold a transaction open during the download and parse

    with tempfile.TemporaryFile() as tmp:
        try:
            with storage.open_read(key) as src:
                copied = 0
                while chunk := src.read(_CHUNK):
                    copied += len(chunk)
                    if copied > MAX_UPLOAD_BYTES:
                        _reject(db, storage, dataset_id, key, "File is larger than the upload limit")
                        return
                    tmp.write(chunk)
        except NotFound:
            _reject(db, storage, dataset_id, key, "Uploaded file is missing; please upload again")
            return
        tmp.seek(0)

        try:
            sheets = inspect_dataset(tmp, PurePosixPath(filename).suffix.lower(), filename)
        except DatasetRejected as exc:
            _reject(db, storage, dataset_id, key, exc.reason)
            return

    if _finish(db, dataset_id, status="ready", sheets=sheets, reject_reason=None):
        logger.info("Dataset id=%s ready (%d sheet(s))", dataset_id, len(sheets))


@celery_app.task(bind=True, name="datasets.process", max_retries=MAX_RETRIES)
def process_dataset(self, dataset_id: str) -> None:
    dataset_uuid = uuid.UUID(dataset_id)
    retrying = False
    try:
        with SessionLocal() as db:
            run_process_dataset(db, get_storage(), dataset_uuid)
    except TRANSIENT_ERRORS as exc:
        if self.request.retries < MAX_RETRIES:
            retrying = True
            raise self.retry(exc=exc, countdown=5 * 2**self.request.retries)
        # Type only: GCS error messages contain object keys (user ids and filenames).
        logger.warning("Giving up on dataset id=%s after %d retries: %s", dataset_id, MAX_RETRIES, type(exc).__name__)
        with SessionLocal() as db:
            dataset = db.get(Dataset, dataset_uuid)
            if dataset is not None:
                _reject(db, get_storage(), dataset.id, dataset.gcs_key, "Processing failed; please upload again")
        raise
    finally:
        if not retrying:  # a scheduled retry still owns the dedupe slot
            redis_client.delete(_enqueued_key(dataset_uuid))


def _enqueued_key(dataset_id: uuid.UUID) -> str:
    return f"dataset:{dataset_id}:enqueued"


def enqueue_process_dataset(dataset_id: uuid.UUID) -> None:
    """Enqueues at most one task per dataset at a time, so repeated /complete calls can't flood workers."""
    key = _enqueued_key(dataset_id)
    if not redis_client.set(key, "1", nx=True, ex=ENQUEUE_DEDUPE_S):
        return
    try:
        process_dataset.delay(str(dataset_id))
    except Exception:
        redis_client.delete(key)
        raise
