"""FastAPI app and routes."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, model_validator
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from app import db, forge, settings
from app.adapter import cv_builder, cv_tailor, extract_text, llm
from app.reader import reader
from app.reader.add_job import AddJobError, add_job
from app.scout.attributions import ATTRIBUTIONS
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


class NoStoreStaticFiles(StaticFiles):
    """Static files that the browser never caches (so a new build is always picked up)."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


app.mount("/static", NoStoreStaticFiles(directory=STATIC_DIR), name="static")


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
    """Upload a CV (the raw file as the request body; pdf, docx, md, or txt). Free: no AI call."""
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
    """Exactly the sanitized text that will be sent to the AI model."""
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
    """The owner's confirmations before a paid AI call."""

    confirm_cost: bool = False
    confirm_contacts: bool = False


@app.post("/api/cv/build", status_code=202, response_model=None)
def cv_build(body: BuildRequest) -> dict[str, bool] | JSONResponse:
    """Build the base CV with one AI call (needs both confirmations)."""
    try:
        cv_builder.start_build(body.confirm_cost, body.confirm_contacts)
    except cv_builder.BuildBlocked as exc:
        return _blocked(exc)
    except llm.LLMError as exc:  # pragma: no cover - raised only by synchronous callers
        return JSONResponse({"detail": str(exc)}, status_code=422)
    return {"started": True}


# --- tailoring (CV Adapter) ------------------------------------------------------------------

JobKey = Annotated[str, Query(min_length=1, max_length=64)]
_MEDIA = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


def _llm_failure(exc: llm.LLMError) -> JSONResponse:
    status = 422 if isinstance(exc, llm.NotConfigured | llm.BudgetExceeded) else 502
    return JSONResponse({"detail": str(exc)}, status_code=status)


@app.get("/api/promote/estimate", response_model=None)
def promote_estimate(key: JobKey) -> dict[str, Any] | JSONResponse:
    """Estimated cost of tailoring the CV to one job (reads the job first; free)."""
    try:
        return cv_tailor.estimate(key)
    except cv_tailor.PromoteBlocked as exc:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)
    except KeyError:
        return JSONResponse({"detail": "Unknown job"}, status_code=404)


class PromoteRequest(BaseModel):
    """Body of POST /api/promote."""

    key: str = Field(min_length=1, max_length=64)
    confirm_cost: bool = False


@app.post("/api/promote", response_model=None)
def promote(body: PromoteRequest) -> dict[str, Any] | JSONResponse:
    """Tailor the CV to one job. An existing tailored CV is reused for free; otherwise needs confirm_cost."""
    try:
        return cv_tailor.promote(body.key, body.confirm_cost)
    except cv_tailor.PromoteBlocked as exc:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)
    except llm.LLMError as exc:
        return _llm_failure(exc)


@app.get("/api/tailored", response_model=None)
def tailored(key: JobKey) -> dict[str, Any] | JSONResponse:
    """A job's tailored CV with its validation result."""
    with db.connect() as conn:
        stored = db.get_tailored(conn, key)
    if stored is None:
        return JSONResponse({"detail": "No tailored CV for this job"}, status_code=404)
    return cv_tailor.describe_result(key, stored, reused=True)


def _download(key: str, kind: str) -> FileResponse | JSONResponse:
    try:
        path, name = cv_tailor.export_file(key, kind)
    except ValueError:
        return JSONResponse({"detail": "Invalid request"}, status_code=400)
    except LookupError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    return FileResponse(path, media_type=_MEDIA[kind], filename=name)


@app.get("/api/cv-docx", response_model=None)
def cv_docx(key: JobKey) -> FileResponse | JSONResponse:
    """Download a tailored CV as .docx."""
    return _download(key, "docx")


@app.get("/api/cv-pdf", response_model=None)
def cv_pdf(key: JobKey) -> FileResponse | JSONResponse:
    """Download a tailored CV as .pdf."""
    return _download(key, "pdf")


# --- UI support -------------------------------------------------------------------------------

_AGENT_ROLES = {
    "forge": "Orchestrator: runs the pipeline on your click",
    "scout": "Finds, de-duplicates, and scores jobs",
    "job-reader": "Reads a job post and extracts its details",
    "cv-adapter": "Builds and tailors your CV (the only agent that uses an AI model, Groq)",
}
LIVE_WINDOW_SECONDS = 15 * 60


def _agent_status(busy: bool, last_at: str | None) -> str:
    """WORKING while busy, LIVE if it acted in the last 15 minutes, else IDLE."""
    if busy:
        return "WORKING"
    if last_at:
        age = (datetime.now(UTC) - datetime.fromisoformat(last_at)).total_seconds()
        if age <= LIVE_WINDOW_SECONDS:
            return "LIVE"
    return "IDLE"


@app.get("/api/agents")
def agents() -> list[dict[str, Any]]:
    """The four agents with their status, task counts, success rate, and last action."""
    find_running = forge.is_active()
    busy = {
        "forge": find_running, "scout": find_running, "job-reader": reader.is_busy(),
        "cv-adapter": cv_builder.is_building() or cv_tailor.is_busy(),
    }
    with db.connect() as conn:
        stats = db.agent_stats(conn)
    return [
        {
            "name": name, "role": _AGENT_ROLES[name], "status": _agent_status(busy[name], stat["last_at"]),
            "tasks": stat["tasks"], "success_rate": round(100 * stat["completed"] / stat["tasks"]) if stat["tasks"] else None,
            "last_action": stat["last_action"], "last_model": stat["last_model"], "last_at": stat["last_at"],
        }
        for name, stat in stats.items()
    ]


@app.get("/api/activity")
def activity(limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict[str, Any]]:
    """The newest activity entries first."""
    with db.connect() as conn:
        return db.list_activity(conn, limit)


@app.get("/api/attributions")
def attributions() -> dict[str, dict[str, str]]:
    """Source credits the page footer must show."""
    return ATTRIBUTIONS


class JobStatusRequest(BaseModel):
    """Body of POST /api/job/status."""

    key: str = Field(min_length=1, max_length=64)
    user_status: Literal["new", "interested", "applied", "interview", "rejected"]


@app.post("/api/job/status", response_model=None)
def set_job_status(body: JobStatusRequest) -> dict[str, str] | JSONResponse:
    """Save your own status for a job."""
    with db.connect() as conn:
        found = db.set_user_status(conn, body.key, body.user_status)
    if not found:
        return JSONResponse({"detail": "Unknown job"}, status_code=404)
    return {"key": body.key, "user_status": body.user_status}


@app.get("/api/jd", response_model=None)
def job_detail(key: Annotated[str, Query(min_length=1, max_length=64)]) -> dict[str, Any] | JSONResponse:
    """What the job description drawer shows."""
    with db.connect() as conn:
        detail = db.get_job_detail(conn, key)
    if detail is None:
        return JSONResponse({"detail": "Unknown job"}, status_code=404)
    return detail


# --- settings ---------------------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    """The effective profile, sources, boards file, AI model settings, and read-only system status."""
    return settings.effective_settings()


@app.post("/api/settings", response_model=None)
def save_settings(body: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    """Save a partial update (only the keys sent change). Numbers are clamped; bad choices return 400."""
    try:
        settings.apply_update(body)
    except settings.SettingsError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return settings.effective_settings()


class BoardsRequest(BaseModel):
    """Body of POST /api/boards: the full text of boards.yaml."""

    yaml: str = Field(max_length=settings.MAX_BOARDS_YAML_CHARS)


@app.post("/api/boards", response_model=None)
def save_boards(body: BoardsRequest) -> dict[str, Any] | JSONResponse:
    """Validate and save boards.yaml."""
    try:
        counts = settings.save_boards_yaml(body.yaml)
    except settings.SettingsError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"saved": True, "boards": counts}


class BoardCheckRequest(BaseModel):
    """Body of POST /api/boards/check."""

    ats: Literal["greenhouse", "lever", "ashby", "workable"]
    slug: str = Field(min_length=1, max_length=64, pattern=SLUG_RE.pattern)


@app.post("/api/boards/check")
def check_board(body: BoardCheckRequest) -> dict[str, Any]:
    """Check whether a company board slug exists (one read-only request to that ATS's public API)."""
    return settings.check_board(body.ats, body.slug)
