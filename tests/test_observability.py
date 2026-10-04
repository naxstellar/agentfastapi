import json
import logging
import uuid

from app.observability import ContextFilter, JsonFormatter, bind_run


def _record(message: str) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None)
    ContextFilter().filter(record)
    return record


def test_unbound_lines_have_dashes():
    bind_run(None, None)
    entry = json.loads(JsonFormatter().format(_record("hello")))
    assert entry["run_id"] == "-" and entry["user_id"] == "-" and entry["message"] == "hello"


def test_bound_ids_are_logged():
    run_id, user_id = uuid.uuid4(), uuid.uuid4()
    bind_run(run_id, user_id)
    try:
        entry = json.loads(JsonFormatter().format(_record("x")))
        assert entry["run_id"] == str(run_id) and entry["user_id"] == str(user_id)
    finally:
        bind_run(None, None)
