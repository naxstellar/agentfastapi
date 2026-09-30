import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import app.models  # noqa: F401 - registers User with Base.metadata
import app.models_agent  # noqa: F401 - registers agent tables (conversations, runs, ...)
from app.auth.routes import router as auth_router
from app.config import SESSION_SECRET_KEY
from app.db import Base, engine
from app.redis_client import async_redis_client
from app.routers.api_v1 import router as api_v1_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield
    await async_redis_client.aclose()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    session_cookie="session",
    same_site="lax",
    https_only=True,
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(auth_router)
app.include_router(api_v1_router)


@app.get("/")
def read_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/hello")
def read_hello() -> dict[str, str]:
    return {"message": "Hello, HTTPS!"}
