"""FastAPI app and routes."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app import db
from app.config import STATIC_DIR
from app.security import local_security


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Create the database and tables on startup."""
    db.init_db()
    yield


# API docs are disabled: no extra routes, and no CDN assets loaded by the browser.
app = FastAPI(title="Forge", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.middleware("http")(local_security)


@app.get("/api/health")
def health() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    """Serve the single-page UI."""
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})
