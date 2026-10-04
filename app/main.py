import mimetypes
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import google.auth.exceptions
import kombu.exceptions
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from google.api_core.exceptions import GoogleAPIError
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import app.models  # noqa: F401 - registers User with Base.metadata
import app.models_agent  # noqa: F401 - registers agent tables (conversations, runs, ...)
from app.auth.routes import router as auth_router
from app.config import SESSION_SECRET_KEY
from app.db import Base, engine
from app.redis_client import async_redis_client
from app.routers.api_v1 import router as api_v1_router
from app.services.events import api_events_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield
    await async_redis_client.aclose()
    await api_events_client().aclose()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    session_cookie="session",
    same_site="lax",
    https_only=True,
)

# Windows maps .js to text/plain via the registry, and browsers refuse such ES modules.
mimetypes.add_type("text/javascript", ".js")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")



# GCS, IAM signing and the Celery broker are external dependencies: their outages are 503s, not 500s.
@app.exception_handler(GoogleAPIError)
@app.exception_handler(google.auth.exceptions.GoogleAuthError)
@app.exception_handler(kombu.exceptions.OperationalError)
async def dependency_unavailable(request: Request, exc: Exception) -> JSONResponse:
    # Type only: GCS error messages contain object keys (user ids and filenames).
    logger.warning("%s %s failed: %s", request.method, request.url.path, type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "A storage or queue service is unavailable; try again"})


app.include_router(auth_router)
app.include_router(api_v1_router)


@app.get("/")
def read_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/hello")
def read_hello() -> dict[str, str]:
    return {"message": "Hello, HTTPS!"}
