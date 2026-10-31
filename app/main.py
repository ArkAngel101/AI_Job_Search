"""FastAPI app and routes."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, model_validator
from fastapi.responses import FileResponse, JSONResponse

from app import db, forge
from app.adapter import cv_builder, extract_text, llm
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


# --- CV (CV Adapter) ---------------------------------------------------------------------

async def _read_capped(request: Request, limit: int) -> bytes | None:
    """The request body, or None if it is larger than `limit` bytes."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/api/cv", response_model=None)
async def upload_cv(request: Request, filename: Annotated[str, Query(min_length=1, max_length=200)]) -> dict[str, Any] | JSONResponse:
    """Upload a CV (the raw file as the request body; pdf, docx, md, or txt). Free: no Claude call."""
    data = await _read_capped(request, extract_text.MAX_UPLOAD_BYTES)
    if data is None:
        return JSONResponse({"detail": "The file is larger than 5 MB"}, status_code=413)
    try:
        return await run_in_threadpool(cv_builder.prepare_upload, data, filename)
    except extract_text.UploadError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)


@app.get("/api/cv")
def cv_status() -> dict[str, Any]:
    """Building flag, base CV preview, and the pending upload with its detected contact details."""
    return cv_builder.get_status()


def _blocked(exc: cv_builder.BuildBlocked) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


@app.get("/api/cv/preview", response_model=None)
def cv_preview() -> dict[str, Any] | JSONResponse:
    """Exactly the sanitized text that will be sent to Claude."""
    try:
        return cv_builder.preview()
    except cv_builder.BuildBlocked as exc:
        return _blocked(exc)


@app.get("/api/cv/estimate", response_model=None)
def cv_estimate() -> dict[str, Any] | JSONResponse:
    """Estimated tokens and cost of building the base CV."""
    try:
        return cv_builder.estimate()
    except cv_builder.BuildBlocked as exc:
        return _blocked(exc)


class ContactItem(BaseModel):
    """A contact detail to add."""

    kind: Literal["name", "email", "phone", "address", "linkedin", "github", "url"]
    value: str = Field(min_length=1, max_length=300)


class ContactsRequest(BaseModel):
    """Owner corrections to the detected contact details."""

    add: list[ContactItem] = Field(default_factory=list, max_length=50)
    remove: list[str] = Field(default_factory=list, max_length=50)
    name: str | None = Field(default=None, max_length=200)


@app.post("/api/cv/contacts", response_model=None)
def cv_contacts(body: ContactsRequest) -> dict[str, Any] | JSONResponse:
    """Add a missed contact detail, remove a wrong one, or set the name."""
    try:
        return cv_builder.update_contacts([i.model_dump() for i in body.add], body.remove, body.name)
    except cv_builder.BuildBlocked as exc:
        return _blocked(exc)


class BuildRequest(BaseModel):
    """The owner's confirmations before a paid Claude call."""

    confirm_cost: bool = False
    confirm_contacts: bool = False


@app.post("/api/cv/build", status_code=202, response_model=None)
def cv_build(body: BuildRequest) -> dict[str, bool] | JSONResponse:
    """Build the base CV with one Claude call (needs both confirmations)."""
    try:
        cv_builder.start_build(body.confirm_cost, body.confirm_contacts)
    except cv_builder.BuildBlocked as exc:
        return _blocked(exc)
    except llm.LLMError as exc:  # pragma: no cover - raised only by synchronous callers
        return JSONResponse({"detail": str(exc)}, status_code=422)
    return {"started": True}
