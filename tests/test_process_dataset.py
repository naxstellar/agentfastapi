import uuid

import pytest

from app.models_agent import Dataset
from app.workers import datasets as worker
from app.workers.datasets import run_process_dataset
from tests.fakes import FakeStorage

CSV = b"a,b\n1,2\n"


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def make_dataset(db, user, storage):
    def _make(data: bytes | None, filename: str = "data.csv", status: str = "pending") -> Dataset:
        dataset = Dataset(
            id=uuid.uuid4(),
            user_id=user.id,
            filename=filename,
            size_bytes=len(data or b""),
            content_type="text/csv",
            status=status,
        )
        dataset.gcs_key = f"users/{user.id}/datasets/{dataset.id}/{filename}"
        if data is not None:
            storage.objects[dataset.gcs_key] = data
        db.add(dataset)
        db.commit()
        return dataset

    return _make


def test_marks_ready(db, storage, make_dataset):
    dataset = make_dataset(CSV)

    run_process_dataset(db, storage, dataset.id)

    db.refresh(dataset)
    assert dataset.status == "ready"
    assert dataset.sheets[0]["columns"] == ["a", "b"]


@pytest.mark.parametrize("status", ["ready", "rejected"])
def test_already_processed_dataset_is_left_alone(db, storage, make_dataset, status):
    dataset = make_dataset(b"\x00garbage", status=status)

    run_process_dataset(db, storage, dataset.id)

    db.refresh(dataset)
    assert dataset.status == status
    assert dataset.gcs_key in storage.objects


def test_missing_object_is_rejected(db, storage, make_dataset):
    dataset = make_dataset(None)

    run_process_dataset(db, storage, dataset.id)

    db.refresh(dataset)
    assert dataset.status == "rejected"
    assert "missing" in dataset.reject_reason


def test_content_over_upload_limit_is_rejected_without_reading_it_all(db, storage, make_dataset, monkeypatch):
    monkeypatch.setattr(worker, "MAX_UPLOAD_BYTES", 4)
    dataset = make_dataset(CSV)

    run_process_dataset(db, storage, dataset.id)

    db.refresh(dataset)
    assert dataset.status == "rejected"
    assert storage.objects == {}


def test_unknown_dataset_is_a_no_op(db, storage):
    run_process_dataset(db, storage, uuid.uuid4())


def test_dataset_deleted_while_processing_is_not_resurrected(db, storage, make_dataset, monkeypatch):
    dataset = make_dataset(CSV)
    dataset_id = dataset.id
    real_inspect = worker.inspect_dataset

    def inspect_then_user_deletes(*args):
        result = real_inspect(*args)
        db.delete(db.get(Dataset, dataset_id))
        db.commit()
        return result

    monkeypatch.setattr(worker, "inspect_dataset", inspect_then_user_deletes)

    run_process_dataset(db, storage, dataset_id)

    assert db.get(Dataset, dataset_id) is None


def test_enqueue_is_deduplicated_until_the_task_finishes(redis_db, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(worker.process_dataset, "delay", sent.append)
    dataset_id = uuid.uuid4()

    worker.enqueue_process_dataset(dataset_id)
    worker.enqueue_process_dataset(dataset_id)  # e.g. /complete spammed

    assert sent == [str(dataset_id)]


def test_failed_enqueue_releases_the_dedupe_slot(redis_db, monkeypatch):
    def broker_down(_):
        raise ConnectionError("broker down")

    monkeypatch.setattr(worker.process_dataset, "delay", broker_down)
    dataset_id = uuid.uuid4()
    with pytest.raises(ConnectionError):
        worker.enqueue_process_dataset(dataset_id)

    sent: list[str] = []
    monkeypatch.setattr(worker.process_dataset, "delay", sent.append)
    worker.enqueue_process_dataset(dataset_id)

    assert sent == [str(dataset_id)]
