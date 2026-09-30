import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / "env" / ".env"
load_dotenv(ENV_PATH)


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def _redis_db_url(base_url: str, db: int) -> str:
    parts = urlsplit(base_url)
    return urlunsplit(parts._replace(path=f"/{db}"))


AUTH0_DOMAIN = os.environ["AUTH0_DOMAIN"]
AUTH0_CLIENT_ID = os.environ["AUTH0_CLIENT_ID"]
AUTH0_CLIENT_SECRET = os.environ["AUTH0_CLIENT_SECRET"]
AUTH0_AUDIENCE = os.environ["AUTH0_AUDIENCE"]
AUTH0_CALLBACK_URL = os.environ["AUTH0_CALLBACK_URL"]
APP_BASE_URL = os.environ["APP_BASE_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
SESSION_SECRET_KEY = os.environ["SESSION_SECRET_KEY"]

AUTH0_ISSUER = f"https://{AUTH0_DOMAIN}/"
AUTH0_JWKS_URL = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"

APP_ENV = os.environ.get("APP_ENV") or "local"
APP_VERSION = os.environ.get("APP_VERSION") or "dev"

# Agent-service secrets are optional at boot so the auth-only app keeps running without them.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or None
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or None
E2B_API_KEY = os.environ.get("E2B_API_KEY") or None

GCS_BUCKET = os.environ.get("GCS_BUCKET") or None
GCS_SIGNER_SERVICE_ACCOUNT = os.environ.get("GCS_SIGNER_SERVICE_ACCOUNT") or None

# Separate Redis DB numbers per concern (sessions/state on REDIS_URL's DB); derived from
# REDIS_URL by default so Compose/K8s overrides of REDIS_URL carry over.
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL") or _redis_db_url(REDIS_URL, 1)
EVENTS_REDIS_URL = os.environ.get("EVENTS_REDIS_URL") or _redis_db_url(REDIS_URL, 2)

DAILY_TOKEN_BUDGET = _int("DAILY_TOKEN_BUDGET", 200_000)
MAX_CONCURRENT_RUNS = _int("MAX_CONCURRENT_RUNS", 2)

ALLOWED_MODELS = [
    m.strip()
    for m in (
        os.environ.get("ALLOWED_MODELS")
        or "anthropic:claude-sonnet-5-5,anthropic:claude-haiku-4-5,openai:gpt-5"
    ).split(",")
    if m.strip()
]
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL") or ALLOWED_MODELS[0]
if DEFAULT_MODEL not in ALLOWED_MODELS:
    raise RuntimeError(f"DEFAULT_MODEL {DEFAULT_MODEL!r} is not in ALLOWED_MODELS")

# JSON object in langchain-mcp-adapters' MultiServerMCPClient format: {"name": {"transport": ..., ...}}
MCP_SERVERS: dict = json.loads(os.environ.get("MCP_SERVERS") or "{}")
if not isinstance(MCP_SERVERS, dict):
    raise RuntimeError("MCP_SERVERS must be a JSON object")

MAX_UPLOAD_BYTES = _int("MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
MAX_USER_STORAGE_BYTES = _int("MAX_USER_STORAGE_BYTES", 1024 * 1024 * 1024)

AGENT_RECURSION_LIMIT = _int("AGENT_RECURSION_LIMIT", 50)
RUN_TIME_LIMIT_S = _int("RUN_TIME_LIMIT_S", 3600)
APPROVAL_TIMEOUT_S = _int("APPROVAL_TIMEOUT_S", 3600)

SANDBOX_IDLE_TIMEOUT_S = _int("SANDBOX_IDLE_TIMEOUT_S", 300)
SANDBOX_EXEC_TIMEOUT_S = _int("SANDBOX_EXEC_TIMEOUT_S", 120)
MAX_CONCURRENT_SANDBOXES = _int("MAX_CONCURRENT_SANDBOXES", 20)

SUMMARIZE_TRIGGER_TOKENS = _int("SUMMARIZE_TRIGGER_TOKENS", 100_000)
SUMMARIZE_KEEP_MESSAGES = _int("SUMMARIZE_KEEP_MESSAGES", 20)
