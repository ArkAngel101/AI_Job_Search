"""FastAPI app and routes."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field, model_validator
from fastapi.responses import FileResponse, JSONResponse

from app import db, forge
from app.reader.add_job import AddJobError, add_job
from app.scout.boards import SLUG_RE, add_board
from app.config import STATIC_DIR
from app.security import local_security


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Create the database and tables on startup; close runs a previous process left unfinished."""
    db.init_db()
    with db.connect() as conn:
        db.fail_stale_runs(conn)
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


@app.post("/api/run", status_code=202, response_model=None)
def start_run() -> dict[str, int] | JSONResponse:
    """Start a Find run in the background (409 if one is already running)."""
    try:
        return {"run_id": forge.start_find("button")}
    except forge.RunActive:
        return JSONResponse({"detail": "A run is already active"}, status_code=409)


@app.get("/api/run-status")
def run_status() -> dict[str, Any]:
    """The current or last run, with per-source progress."""
    return forge.get_status()


@app.get("/api/jobs")
def jobs(
    min_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    new_only: bool = False,
    hide_closed: bool = False,
    status: Literal["seen", "matched", "read", "tailored"] | None = None,
    user_status: Literal["new", "interested", "applied", "interview", "rejected"] | None = None,
    source: Annotated[str | None, Query(max_length=40)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    """Ranked job rows, best score first."""
    with db.connect() as conn:
        return db.list_jobs(
            conn, min_score=min_score, new_only=new_only, hide_closed=hide_closed, status=status,
            user_status=user_status, source=source, q=q, limit=limit, offset=offset,
        )


@app.get("/api/overview")
def overview() -> dict[str, int]:
    """Funnel counts."""
    with db.connect() as conn:
        return db.overview_counts(conn)


class AddJobRequest(BaseModel):
    """Body of POST /api/add-job: a URL, pasted text, or both (text wins)."""

    url: str | None = Field(default=None, max_length=2000)
    text: str | None = Field(default=None, max_length=100_000)
    title: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def need_url_or_text(self) -> "AddJobRequest":
        if not (self.url or "").strip() and not (self.text or "").strip():
            raise ValueError("Provide a job URL or the job description text")
        return self


@app.post("/api/add-job", response_model=None)
def add_job_endpoint(body: AddJobRequest) -> dict[str, Any] | JSONResponse:
    """Add a job by pasted URL or text; Job Reader reads it (no LLM)."""
    try:
        return add_job(body.url, body.text, body.title, body.company, body.location)
    except AddJobError as exc:
        return JSONResponse({"detail": str(exc), "needs_text": exc.needs_text}, status_code=exc.status)


class AddBoardRequest(BaseModel):
    """Body of POST /api/add-board."""

    ats: Literal["greenhouse", "lever", "ashby", "workable"]
    slug: str = Field(max_length=64, pattern=SLUG_RE.pattern)
    name: str | None = Field(default=None, max_length=100)


@app.post("/api/add-board")
def add_board_endpoint(body: AddBoardRequest) -> dict[str, bool]:
    """Add a company board (offered after a pasted ATS link) to boards.yaml."""
    return {"added": add_board(body.ats, body.slug, body.name)}
