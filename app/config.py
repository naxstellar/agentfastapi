import json
import os
import re
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
MODEL_PROVIDERS = ("anthropic", "openai")
for _model in ALLOWED_MODELS:
    if _model.partition(":")[0] not in MODEL_PROVIDERS or not _model.partition(":")[2]:
        raise RuntimeError(f"ALLOWED_MODELS entry {_model!r} must look like <{'|'.join(MODEL_PROVIDERS)}>:<model>")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL") or ALLOWED_MODELS[0]
if DEFAULT_MODEL not in ALLOWED_MODELS:
    raise RuntimeError(f"DEFAULT_MODEL {DEFAULT_MODEL!r} is not in ALLOWED_MODELS")

# JSON object in langchain-mcp-adapters' MultiServerMCPClient format: {"name": {"transport": ..., ...}}
MCP_SERVERS: dict = json.loads(os.environ.get("MCP_SERVERS") or "{}")
if not isinstance(MCP_SERVERS, dict):
    raise RuntimeError("MCP_SERVERS must be a JSON object")

MAX_UPLOAD_BYTES = _int("MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
# Datasets travel base64-encoded inside one sandbox execute request, capped at 100 MB by the service.
if MAX_UPLOAD_BYTES > 70 * 1024 * 1024:
    raise RuntimeError("MAX_UPLOAD_BYTES must be at most 70 MiB (the sandbox request cap)")
MAX_USER_STORAGE_BYTES = _int("MAX_USER_STORAGE_BYTES", 1024 * 1024 * 1024)

AGENT_RECURSION_LIMIT = _int("AGENT_RECURSION_LIMIT", 50)
RUN_TIME_LIMIT_S = _int("RUN_TIME_LIMIT_S", 3600)
APPROVAL_TIMEOUT_S = _int("APPROVAL_TIMEOUT_S", 3600)

# Code execution: Vertex AI Agent Engine. Sandboxes live under this instance, and the API location
# is taken from its name. Optional at boot; without it make_sandbox_manager() raises SandboxNotConfiguredError.
AGENT_ENGINE_NAME = os.environ.get("AGENT_ENGINE_NAME") or None
if AGENT_ENGINE_NAME and not re.fullmatch(r"projects/[^/]+/locations/[^/]+/reasoningEngines/[^/]+", AGENT_ENGINE_NAME):
    raise RuntimeError("AGENT_ENGINE_NAME must look like projects/<p>/locations/<l>/reasoningEngines/<id>")
SANDBOX_MACHINE_CONFIG = os.environ.get("SANDBOX_MACHINE_CONFIG") or None
if SANDBOX_MACHINE_CONFIG not in (None, "MACHINE_CONFIG_UNSPECIFIED", "MACHINE_CONFIG_VCPU4_RAM4GIB"):
    raise RuntimeError("SANDBOX_MACHINE_CONFIG must be MACHINE_CONFIG_UNSPECIFIED or MACHINE_CONFIG_VCPU4_RAM4GIB")
# A sandbox's TTL is fixed at creation (executions don't extend it), so this is its maximum lifetime.
SANDBOX_TTL_S = _int("SANDBOX_TTL_S", 3600)
# Sandboxes unused this long may be deleted early to free capacity.
SANDBOX_IDLE_TIMEOUT_S = _int("SANDBOX_IDLE_TIMEOUT_S", 300)
SANDBOX_EXEC_TIMEOUT_S = _int("SANDBOX_EXEC_TIMEOUT_S", 120)
# The service ends an execution after ~300 s with a 503; our own deadline must always fire first.
if not 0 < SANDBOX_EXEC_TIMEOUT_S < 290:
    raise RuntimeError("SANDBOX_EXEC_TIMEOUT_S must be between 1 and 289")
if SANDBOX_TTL_S < 2 * SANDBOX_EXEC_TIMEOUT_S + 600:
    raise RuntimeError("SANDBOX_TTL_S must be at least 2 * SANDBOX_EXEC_TIMEOUT_S + 600")
# Below the Agent Engine quota of 500 sandboxes per project and region.
MAX_CONCURRENT_SANDBOXES = _int("MAX_CONCURRENT_SANDBOXES", 450)
MAX_ARTIFACT_BYTES = _int("MAX_ARTIFACT_BYTES", 25 * 1024 * 1024)

MAX_MEMORIES = _int("MAX_MEMORIES", 200)  # per user
MAX_MEMORY_CHARS = _int("MAX_MEMORY_CHARS", 500)
RECENT_MEMORIES_INJECTED = _int("RECENT_MEMORIES_INJECTED", 20)

SUMMARIZE_TRIGGER_TOKENS =_int("SUMMARIZE_TRIGGER_TOKENS", 100_000)
SUMMARIZE_KEEP_MESSAGES = _int("SUMMARIZE_KEEP_MESSAGES", 20)
