import uuid
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated

import google.auth.exceptions
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth.security import DbUserDep
from app.config import MAX_UPLOAD_BYTES, MAX_USER_STORAGE_BYTES
from app.db import DbSessionDep
from app.models import User
from app.models_agent import Conversation, ConversationDataset, Dataset
from app.services.dataset_inspect import CONTENT_TYPES
from app.services.storage import (
    PENDING_PREFIX,
    SignedUrl,
    Storage,
    StorageNotConfiguredError,
    dataset_key,
    get_storage,
    pending_dataset_key,
)
from app.workers.datasets import enqueue_process_dataset

router = APIRouter(tags=["datasets"])


def _storage() -> Storage:
    try:
        return get_storage()
    except (StorageNotConfiguredError, google.auth.exceptions.DefaultCredentialsError) as exc:
        raise HTTPException(status_code=503, detail="File storage is not configured") from exc


StorageDep = Annotated[Storage, Depends(_storage)]


class SheetInfo(BaseModel):
    name: str
    columns: list[str]
    row_count: int
    rows_truncated: bool


class DatasetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    size_bytes: int
    content_type: str
    status: str
    sheets: list[SheetInfo] | None
    reject_reason: str | None
    created_at: datetime


class DatasetCreate(BaseModel):
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    size_bytes: Annotated[int, Field(gt=0)]


class SignedUrlOut(BaseModel):
    url: str
    method: str
    headers: dict[str, str]
    expires_at: datetime


class DatasetUpload(BaseModel):
    dataset: DatasetOut
    upload: SignedUrlOut


class AttachDataset(BaseModel):
    dataset_id: uuid.UUID


def _signed(url: SignedUrl, method: str) -> SignedUrlOut:
    return SignedUrlOut(url=url.url, method=method, headers=url.headers, expires_at=url.expires_at)


def _owned_dataset(db: Session, user: User, dataset_id: uuid.UUID, *, lock: bool = False) -> Dataset:
    query = select(Dataset).where(Dataset.id == dataset_id, Dataset.user_id == user.id)
    if lock:
        query = query.with_for_update()
    dataset = db.scalars(query).one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset


def _owned_conversation(db: Session, user: User, conversation_id: uuid.UUID) -> Conversation:
    conversation = db.scalars(
        select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id)
    ).one_or_none()
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.post("/datasets", status_code=201)
def create_dataset(body: DatasetCreate, user: DbUserDep, db: DbSessionDep, storage: StorageDep) -> DatasetUpload:
    filename = PurePosixPath(body.filename.replace("\\", "/")).name.strip()
    extension = PurePosixPath(filename).suffix.lower()
    if extension not in CONTENT_TYPES:
        allowed = ", ".join(sorted(CONTENT_TYPES))
        raise HTTPException(status_code=415, detail=f"Unsupported file type; allowed: {allowed}")
    if body.size_bytes > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File is larger than the {MAX_UPLOAD_BYTES}-byte upload limit")

    # Lock the user row so concurrent uploads can't both pass the storage-cap check.
    db.scalars(select(User).where(User.id == user.id).with_for_update()).one()
    used = db.scalar(
        select(func.coalesce(func.sum(Dataset.size_bytes), 0)).where(
            Dataset.user_id == user.id, Dataset.status != "rejected"
        )
    )
    if used + body.size_bytes > MAX_USER_STORAGE_BYTES:
        raise HTTPException(status_code=413, detail="Storage quota exceeded; delete some datasets first")

    content_type = CONTENT_TYPES[extension]
    dataset = Dataset(
        id=uuid.uuid4(),
        user_id=user.id,
        filename=filename,
        size_bytes=body.size_bytes,
        content_type=content_type,
        status="pending",
    )
    dataset.gcs_key = pending_dataset_key(user.id, dataset.id, filename)
    # The signed range is the declared size, so the declared size is what counts against the quota.
    upload = storage.upload_url(dataset.gcs_key, content_type, body.size_bytes)
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return DatasetUpload(dataset=DatasetOut.model_validate(dataset), upload=_signed(upload, "PUT"))


@router.post("/datasets/{dataset_id}/complete", status_code=202)
def complete_dataset(dataset_id: uuid.UUID, user: DbUserDep, db: DbSessionDep, storage: StorageDep) -> DatasetOut:
    dataset = _owned_dataset(db, user, dataset_id, lock=True)
    if dataset.status != "pending":
        return DatasetOut.model_validate(dataset)

    if dataset.gcs_key.startswith(PENDING_PREFIX):
        final_key = dataset_key(user.id, dataset.id, dataset.filename)
        size = storage.object_size(dataset.gcs_key)
        if size is not None:
            storage.move(dataset.gcs_key, final_key)
        else:
            # A previous /complete may have moved the object but failed before committing.
            size = storage.object_size(final_key)
            if size is None:
                raise HTTPException(status_code=409, detail="Upload not found; PUT the file to the upload URL first")
        dataset.gcs_key = final_key
        dataset.size_bytes = size
        db.commit()

    # Enqueued on every call while still pending, so a retry recovers from a failed enqueue.
    enqueue_process_dataset(dataset.id)
    db.refresh(dataset)
    return DatasetOut.model_validate(dataset)


@router.get("/datasets")
def list_datasets(user: DbUserDep, db: DbSessionDep) -> list[DatasetOut]:
    datasets = db.scalars(
        select(Dataset).where(Dataset.user_id == user.id).order_by(Dataset.created_at.desc())
    ).all()
    return [DatasetOut.model_validate(d) for d in datasets]


@router.get("/datasets/{dataset_id}")
def read_dataset(dataset_id: uuid.UUID, user: DbUserDep, db: DbSessionDep) -> DatasetOut:
    return DatasetOut.model_validate(_owned_dataset(db, user, dataset_id))


@router.get("/datasets/{dataset_id}/download")
def download_dataset(dataset_id: uuid.UUID, user: DbUserDep, db: DbSessionDep, storage: StorageDep) -> SignedUrlOut:
    dataset = _owned_dataset(db, user, dataset_id)
    if dataset.status != "ready":
        raise HTTPException(status_code=409, detail=f"Dataset is {dataset.status}, not ready")
    return _signed(storage.download_url(dataset.gcs_key, dataset.filename), "GET")


@router.delete("/datasets/{dataset_id}", status_code=204)
def delete_dataset(dataset_id: uuid.UUID, user: DbUserDep, db: DbSessionDep, storage: StorageDep) -> None:
    dataset = _owned_dataset(db, user, dataset_id, lock=True)
    # Objects first: if that fails the row survives and the delete can be retried. Both keys, because
    # a /complete that moved the object but failed to commit leaves it at the final key.
    final_key = dataset_key(user.id, dataset.id, dataset.filename)
    storage.delete(PENDING_PREFIX + final_key)
    storage.delete(final_key)
    db.delete(dataset)
    db.commit()


@router.get("/conversations/{conversation_id}/datasets")
def list_attached_datasets(conversation_id: uuid.UUID, user: DbUserDep, db: DbSessionDep) -> list[DatasetOut]:
    conversation = _owned_conversation(db, user, conversation_id)
    datasets = db.scalars(
        select(Dataset)
        .join(ConversationDataset, ConversationDataset.dataset_id == Dataset.id)
        .where(ConversationDataset.conversation_id == conversation.id, Dataset.user_id == user.id)
        .order_by(Dataset.created_at)
    ).all()
    return [DatasetOut.model_validate(d) for d in datasets]


@router.post("/conversations/{conversation_id}/datasets", status_code=204)
def attach_dataset(conversation_id: uuid.UUID, body: AttachDataset, user: DbUserDep, db: DbSessionDep) -> None:
    conversation = _owned_conversation(db, user, conversation_id)
    dataset = _owned_dataset(db, user, body.dataset_id)
    if dataset.status != "ready":
        raise HTTPException(status_code=409, detail=f"Dataset is {dataset.status}, not ready")
    db.execute(
        insert(ConversationDataset)
        .values(conversation_id=conversation.id, dataset_id=dataset.id)
        .on_conflict_do_nothing()
    )
    db.commit()


@router.delete("/conversations/{conversation_id}/datasets/{dataset_id}", status_code=204)
def detach_dataset(conversation_id: uuid.UUID, dataset_id: uuid.UUID, user: DbUserDep, db: DbSessionDep) -> None:
    conversation = _owned_conversation(db, user, conversation_id)
    dataset = _owned_dataset(db, user, dataset_id)
    db.execute(
        delete(ConversationDataset).where(
            ConversationDataset.conversation_id == conversation.id,
            ConversationDataset.dataset_id == dataset.id,
        )
    )
    db.commit()
