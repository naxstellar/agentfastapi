"""Logging with run_id/user_id on every line, in text or JSON (LOG_FORMAT=json)."""
import contextvars
import json
import logging
import os
import uuid

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)


def bind_run(run_id: uuid.UUID | str | None, user_id: uuid.UUID | str | None) -> None:
    """Tags this task's (and the threads it starts) log lines. Set it inside the coroutine/request that owns the run."""
    _run_id.set(str(run_id) if run_id else None)
    _user_id.set(str(user_id) if user_id else None)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get() or "-"
        record.user_id = _user_id.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "run_id": getattr(record, "run_id", "-"),
            "user_id": getattr(record, "user_id", "-"),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


TEXT_FORMAT = "%(asctime)s %(name)s %(levelname)s [run=%(run_id)s user=%(user_id)s]: %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if os.getenv("LOG_FORMAT", "text").lower() == "json" else logging.Formatter(TEXT_FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
