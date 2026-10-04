import time
import uuid

from app.services import run_guard
from app.services.run_guard import Admission


def test_leaked_entries_age_out_instead_of_pinning_the_user_at_the_cap(fake_redis, monkeypatch):
    monkeypatch.setattr(run_guard, "MAX_CONCURRENT_RUNS", 1)
    user_id = uuid.uuid4()
    # A crashed worker's run: its entry expired a minute ago and nobody released it.
    fake_redis.zadd(run_guard._user_key(user_id), {str(uuid.uuid4()): time.time() - 60})
    assert run_guard.acquire(fake_redis, user_id, uuid.uuid4(), uuid.uuid4()) is Admission.OK


def test_release_is_idempotent_and_only_for_the_owner(fake_redis):
    user_id, conversation_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert run_guard.acquire(fake_redis, user_id, conversation_id, run_id) is Admission.OK
    run_guard.release(fake_redis, user_id, conversation_id, uuid.uuid4())  # someone else's run id
    assert run_guard.acquire(fake_redis, user_id, conversation_id, uuid.uuid4()) is Admission.CONVERSATION_BUSY
    run_guard.release(fake_redis, user_id, conversation_id, run_id)
    run_guard.release(fake_redis, user_id, conversation_id, run_id)
    assert run_guard.acquire(fake_redis, user_id, conversation_id, uuid.uuid4()) is Admission.OK
