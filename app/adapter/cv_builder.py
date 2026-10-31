"""CV Adapter, part 1: upload a CV, sanitize it, and build the structured base CV with one Claude call.

Flow: upload (free: extract text, detect contact details) -> owner reviews detected details, previews the
exact text that will be sent, and sees the estimated cost -> owner confirms -> one Claude call -> code
validates and re-attaches the real name and contact details -> the base CV is replaced atomically, and
only if the new one is valid. A failed build leaves the previous base CV untouched.
"""
import json
import os
import re
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app import config, db
from app.adapter import contacts, extract_text, llm
from app.adapter.contacts import Item
from app.adapter.schema import BaseCV, Contact, CVStructured, SuggestedProfile

SENIORITY_VALUES = ("entry", "mid", "senior", "lead", "principal", "manager")
LIMITS = {"titles": (10, 6), "keywords": (12, 2), "scoring_skills": (40, 4)}  # (max items, max words per item)

_lock = threading.Lock()
_state: dict[str, Any] = {"building": False, "error": None}


class BuildBlocked(Exception):
    """The build cannot start (message is safe to show). `status` is the HTTP status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# --- paths and files ------------------------------------------------------------------

def cv_dir() -> Path:
    """Folder holding the CV files (inside the data directory)."""
    return config.DATA_DIR / "cv"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_prompt() -> str:
    """The system prompt for CV structuring."""
    return (config.APP_ROOT / "prompts" / "cv_builder.md").read_text(encoding="utf-8")


# --- upload -----------------------------------------------------------------------------

def prepare_upload(data: bytes, filename: str) -> dict[str, Any]:
    """Validate and store an uploaded CV, extract its text, and detect contact details. No LLM, no cost."""
    extension = extract_text.check_upload(filename, data)
    folder = cv_dir() / "current"
    folder.mkdir(parents=True, exist_ok=True)
    staged = folder / f".upload.{extension}"
    staged.write_bytes(data)
    try:
        text = extract_text.extract_text(staged, extension)
    except extract_text.UploadError:
        staged.unlink(missing_ok=True)  # the previous CV and base CV stay as they were
        raise
    archive = cv_dir() / "archive"
    for old in folder.glob("cv.*"):  # keep the previous raw file instead of overwriting it
        archive.mkdir(parents=True, exist_ok=True)
        os.replace(old, archive / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}{old.suffix}")
    os.replace(staged, folder / f"cv.{extension}")

    items = contacts.detect(text)
    _write_atomic(cv_dir() / "pending.txt", text)
    _save_pending({
        "filename": re.sub(r"[^\w .()-]", "_", Path(filename).name)[:120],
        "uploaded_at": db.now_iso(), "items": [asdict(i) for i in items], "kept": [],
    })
    with _lock:
        _state["error"] = None
    return get_status()


def _save_pending(meta: dict[str, Any]) -> None:
    _write_atomic(cv_dir() / "pending.json", json.dumps(meta))


def _pending() -> tuple[str, dict[str, Any]] | None:
    meta = _read_json(cv_dir() / "pending.json")
    path = cv_dir() / "pending.txt"
    if not meta or not path.exists():
        return None
    return path.read_text(encoding="utf-8"), meta


def _items(meta: dict[str, Any]) -> list[Item]:
    return [Item(i["kind"], i["value"]) for i in meta["items"]]


# --- contact review -----------------------------------------------------------------------

def update_contacts(
    add: list[dict[str, str]] | None = None, remove: list[str] | None = None, name: str | None = None
) -> dict[str, Any]:
    """Owner corrections: add a missed detail, remove a wrong one, or set the name. Raises BuildBlocked."""
    pending = _pending()
    if pending is None:
        raise BuildBlocked("Upload a CV first", 409)
    text, meta = pending
    items = _items(meta)
    kept = set(meta.get("kept", []))

    for value in remove or []:
        match = [i for i in items if i.value.lower() == value.strip().lower()]
        if not match:
            raise BuildBlocked(f"'{value}' is not one of the detected details", 404)
        items = [i for i in items if i not in match]
        kept.add(match[0].value)  # the owner chose to keep it, so the pre-send check will not block on it

    additions = [{"kind": "name", "value": name}] if name and name.strip() else []
    for entry in [*additions, *(add or [])]:
        kind, value = entry.get("kind", ""), " ".join(entry.get("value", "").split())
        if kind not in contacts.KINDS or not value or len(value) > 300:
            raise BuildBlocked("Each added detail needs a valid kind and a value (max 300 characters)", 422)
        if not re.search(r"\s+".join(map(re.escape, value.split())), text, re.IGNORECASE):
            raise BuildBlocked(f"'{value}' does not appear in the CV text", 422)
        if kind == "name":
            items = [i for i in items if i.kind != "name"]
            items.insert(0, Item("name", value))
        elif not any(i.value.lower() == value.lower() for i in items):
            items.append(Item(kind, value))
        kept.discard(value)

    meta.update(items=[asdict(i) for i in items], kept=sorted(kept))
    _save_pending(meta)
    return get_status()


# --- what will be sent ------------------------------------------------------------------------

def build_user_message(sanitized: str) -> str:
    """The exact user message sent to Claude (the preview shows this same string)."""
    return f"<cv>\n{sanitized}\n</cv>"


def preview() -> dict[str, Any]:
    """The sanitized text exactly as it would be sent, plus the placeholders in it."""
    pending = _pending()
    if pending is None:
        raise BuildBlocked("Upload a CV first", 409)
    text, meta = pending
    sanitized = contacts.sanitize(text, _items(meta))
    return {
        "text": sanitized, "message": build_user_message(sanitized), "placeholders": contacts.used_tokens(sanitized),
        "chars": len(sanitized), "possible_leftovers": contacts.leftovers(sanitized, set(meta.get("kept", []))),
    }


def estimate() -> dict[str, Any]:
    """Estimated tokens and cost for building the base CV (shown before the owner confirms)."""
    shown = preview()
    expected_output = min(max(int(len(shown["message"]) / 3.5), 800), 12000)
    return llm.estimate(load_prompt(), shown["message"], expected_output) | {
        "monthly_cap_usd": _setting("monthly_cap_usd"), "spent_this_month_usd": _spent(),
    }


def _setting(key: str) -> Any:
    with db.connect() as conn:
        return db.get_setting(conn, key)


def _spent() -> float:
    with db.connect() as conn:
        return db.month_spend(conn)


# --- status -------------------------------------------------------------------------------------

def get_status() -> dict[str, Any]:
    """Building flag, last error, the base CV, and the pending upload with its detected details."""
    base = _read_json(cv_dir() / "base_cv.json")
    pending = _pending()
    detected = None
    if pending:
        _, meta = pending
        items = _items(meta)
        detected = {
            "filename": meta["filename"], "uploaded_at": meta["uploaded_at"],
            "name": next((i.value for i in items if i.kind == "name"), None),
            "items": [{"kind": i.kind, "value": i.value, "placeholder": t} for i, t in contacts.tokens_for(items).items()],
        }
    with _lock:
        building, error = bool(_state["building"]), _state["error"]
    return {"building": building, "error": error, "has_base": base is not None, "base_cv": base, "pending": detected}


# --- build --------------------------------------------------------------------------------------

def start_build(confirm_cost: bool, confirm_contacts: bool, background: bool = True) -> None:
    """Start the one Claude call that builds the base CV. Raises BuildBlocked if it cannot start."""
    if not (confirm_cost and confirm_contacts):
        raise BuildBlocked("Confirm the estimated cost and the detected contact details before building", 400)
    pending = _pending()
    if pending is None:
        raise BuildBlocked("Upload a CV first", 409)
    text, meta = pending
    items = _items(meta)
    if not any(i.kind == "name" for i in items):
        raise BuildBlocked("No name was detected. Enter your name on the CV page so it can be kept out of the prompt", 422)
    left = contacts.leftovers(contacts.sanitize(text, items), set(meta.get("kept", [])))
    if left:
        raise BuildBlocked(
            f"These look like contact details that are still in the text: {', '.join(left[:5])}. "
            "Add them as detected details (or keep them on purpose) before building", 422)
    with _lock:
        if _state["building"]:
            raise BuildBlocked("A build is already running", 409)
        _state.update(building=True, error=None)
    if background:
        threading.Thread(target=_run_build, name="cv-build", daemon=True).start()
    else:
        _run_build()


def _run_build() -> None:
    pending = _pending()
    filename = pending[1]["filename"] if pending else ""
    error: str | None = None
    try:
        _build()
    except (llm.LLMError, BuildBlocked) as exc:
        error = str(exc)
    except (ValidationError, ValueError) as exc:
        error = f"The structured CV was not valid: {_first_problem(exc)}"
        db.log_activity("cv-adapter", "Base CV build failed validation", "failed", llm.model_name())
    except Exception:  # noqa: BLE001 - keep the owner informed without leaking details
        error = "The build failed unexpectedly. The previous base CV was kept."
        db.log_activity("cv-adapter", "Base CV build crashed", "failed", llm.model_name())
    if error:  # the previous base CV and the pending upload are untouched, so the owner can retry
        with db.connect() as conn:
            db.insert_cv_version(conn, filename, None, False)
    with _lock:
        _state.update(building=False, error=error)


def _first_problem(exc: Exception) -> str:
    if isinstance(exc, ValidationError) and exc.errors():
        first = exc.errors()[0]
        return f"{'.'.join(str(p) for p in first['loc']) or 'CV'}: {first['msg']}"
    return str(exc)


def _build() -> None:
    pending = _pending()
    if pending is None:
        raise BuildBlocked("Upload a CV first", 409)
    text, meta = pending
    items = _items(meta)
    message = build_user_message(contacts.sanitize(text, items))
    structured = llm.structured_call(
        purpose="Structure CV", system=load_prompt(), user=message, schema=CVStructured,
        expected_output_tokens=min(max(int(len(message) / 3.5), 800), 12000),
    )
    base = _reattach(structured, items)  # raises ValidationError if the result is not a usable CV

    folder = cv_dir()
    _write_atomic(folder / "base_cv.json", base.model_dump_json(indent=2))
    _write_atomic(folder / "base_cv.txt", text)
    with db.connect() as conn:
        db.insert_cv_version(conn, meta["filename"], base.model_dump_json(), True)
    for leftover in ("pending.txt", "pending.json"):
        (folder / leftover).unlink(missing_ok=True)
    try:
        merge_suggestions(structured.suggested_profile)
    except Exception:  # noqa: BLE001 - best effort: suggestions must never break the base-CV build
        db.log_activity("cv-adapter", "Could not apply profile suggestions", "failed", llm.model_name())


def _reattach(structured: CVStructured, items: list[Item]) -> BaseCV:
    """Put the real name and contact details back (done in code; Claude never saw them)."""

    def fix(value: Any) -> Any:
        if isinstance(value, str):
            return contacts.restore(value, items)
        if isinstance(value, list):
            return [fix(v) for v in value]
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        return value

    data = fix(structured.model_dump(exclude={"suggested_profile"}))
    name = next((i.value for i in items if i.kind == "name"), "")
    if not data["name"].strip() or data["name"].strip().lower() in {"[name]", "name"}:
        data["name"] = name
    by_kind = {kind: [i.value for i in items if i.kind == kind] for kind in contacts.KINDS}
    data["contact"] = Contact(
        emails=by_kind["email"], phones=by_kind["phone"], addresses=by_kind["address"],
        linkedin=by_kind["linkedin"], github=by_kind["github"], urls=by_kind["url"],
    ).model_dump()
    return BaseCV.model_validate(data)


# --- profile suggestions -----------------------------------------------------------------------------

def merge_suggestions(suggested: SuggestedProfile) -> None:
    """Store CV-derived suggestions. They are merged on top of defaults only for settings the owner has
    not changed (see profile.load_profile), so edits made in Settings are never overwritten."""
    cleaned: dict[str, Any] = {}
    for key, (max_items, max_words) in LIMITS.items():
        values = [" ".join(v.split()) for v in getattr(suggested, key)]
        cleaned[key] = list(dict.fromkeys(v for v in values if v and len(v.split()) <= max_words))[:max_items]
    level = suggested.seniority.strip().lower()
    if level in SENIORITY_VALUES:
        cleaned["seniority"] = level
    with db.connect() as conn:
        db.set_setting(conn, "cv_suggestions", cleaned)
    db.log_activity("cv-adapter", f"Profile suggestions saved ({len(cleaned['scoring_skills'])} skills)", "completed", llm.model_name())


def wait_for_build(timeout: float = 10.0) -> None:
    """Block until a background build finishes (used by tests)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _lock:
            if not _state["building"]:
                return
        time.sleep(0.01)
