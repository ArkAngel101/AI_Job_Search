"""CV Adapter, part 2: tailor the base CV to one chosen job (one paid click per job).

Promote flow: read the job (free, cached) -> reuse an existing tailored CV (costs nothing) -> otherwise show
the estimated cost and require confirmation -> one AI call that may rewrite only the headline, the summary,
and each position's bullets -> code merges those onto the base CV and validates the result against the base
CV text (one retry with the violations listed) -> save, export to .docx and .pdf, mark the job tailored.
"""
import json
import threading
from pathlib import Path
from typing import Any

from app import config, db
from app.adapter import contacts, export, llm, validator
from app.adapter.contacts import Item
from app.adapter.cv_builder import cv_dir
from app.adapter.schema import BaseCV, TailoredCV
from app.profile import load_profile
from app.reader import reader

_promote_lock = threading.Lock()


class PromoteBlocked(Exception):
    """Promote cannot run (message is safe to show). `status` is the HTTP status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def load_prompt() -> str:
    """The system prompt for tailoring."""
    return (config.APP_ROOT / "prompts" / "cv_tailor.md").read_text(encoding="utf-8")


# --- inputs ---------------------------------------------------------------------------

def _load_base() -> tuple[BaseCV, str]:
    path = cv_dir() / "base_cv.json"
    if not path.exists() or not (cv_dir() / "base_cv.txt").exists():
        raise PromoteBlocked("Build your base CV first (CV page)", 409)
    return BaseCV.model_validate_json(path.read_text(encoding="utf-8")), (cv_dir() / "base_cv.txt").read_text(encoding="utf-8")


def _items(base: BaseCV) -> list[Item]:
    """The name and every contact detail of the base CV, as sanitizer items."""
    c = base.contact
    groups = [("email", c.emails), ("phone", c.phones), ("address", c.addresses), ("linkedin", c.linkedin),
              ("github", c.github), ("url", c.urls)]
    return [Item("name", base.name), *(Item(kind, value) for kind, values in groups for value in values)]


def _sanitize_value(value: Any, items: list[Item]) -> Any:
    if isinstance(value, str):
        return contacts.sanitize(value, items)
    if isinstance(value, list):
        return [_sanitize_value(v, items) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_value(v, items) for k, v in value.items()}
    return value


def build_user_message(base: BaseCV, jd: dict[str, Any], title: str, company: str) -> str:
    """The user message: the job (untrusted text, in tags) and the base CV without name or contact details."""
    items = _items(base)
    cv = _sanitize_value(base.model_dump(exclude={"name", "contact"}), items)
    posting = "\n".join([
        f"Title: {title}", f"Company: {company}",
        f"Required skills: {', '.join(jd['required_skills']) or 'not listed'}",
        f"Preferred skills: {', '.join(jd['preferred_skills']) or 'not listed'}",
        "Responsibilities:", *(f"- {r}" for r in jd["responsibilities"]),
        "Full description:", jd["text"],
    ])
    return f"<job>\n{posting}\n</job>\n\n<cv>\n{json.dumps(cv, indent=2)}\n</cv>"


# --- merge ----------------------------------------------------------------------------------

def merge(base: BaseCV, tailored: TailoredCV) -> dict[str, Any]:
    """Apply the three rewritable fields onto the base CV. Employers, roles, dates, and education never change."""
    items = _items(base)
    by_key = {validator.role_key(r.company, r.role, r.start): r for r in tailored.experience}
    merged = base.model_dump()
    merged["headline"] = contacts.restore(tailored.headline, items)
    merged["summary"] = contacts.restore(tailored.summary, items)
    for entry in merged["experience"]:
        rewritten = by_key.get(validator.role_key(entry["company"], entry["role"], entry["start"]))
        if rewritten is not None and len(rewritten.bullets) == len(entry["bullets"]):
            entry["bullets"] = [contacts.restore(b, items) for b in rewritten.bullets]
    return merged


# --- estimate and promote -----------------------------------------------------------------------

def _context(key: str) -> tuple[BaseCV, str, dict[str, Any], dict[str, Any]]:
    """Base CV, its text, the job's JD (read now if needed; free), and the job summary row."""
    with db.connect() as conn:
        summary = db.get_job_summary(conn, key)
    if summary is None:
        raise PromoteBlocked("Unknown job", 404)
    base, base_text = _load_base()
    jd = reader.read_job(key)
    return base, base_text, jd, summary


def estimate(key: str) -> dict[str, Any]:
    """Cost estimate shown before the owner confirms. A job that already has a tailored CV costs nothing."""
    with db.connect() as conn:
        existing = db.get_tailored(conn, key)
    if existing:
        return {"reused": True, "cost_usd": 0.0, "max_cost_usd": 0.0}
    base, _, jd, summary = _context(key)
    message = build_user_message(base, jd, summary["title"], summary["company"])
    expected = min(max(int(len(json.dumps([e.bullets for e in base.experience])) / 3.5) + 200, 600), 6000)
    one_call = llm.estimate(load_prompt(), message, expected)
    cost = one_call["cost_usd"]
    return {
        "reused": False, **one_call, "max_cost_usd": None if cost is None else cost * 2,
        "note": "A second call is made only if the first answer fails validation.",
        "monthly_cap_usd": _setting("monthly_cap_usd"),
    }


def _setting(name: str) -> Any:
    with db.connect() as conn:
        return db.get_setting(conn, name)


def promote(key: str, confirm_cost: bool) -> dict[str, Any]:
    """Tailor the base CV to one job. Reuses an existing result; otherwise needs confirm_cost and spends tokens."""
    if not _promote_lock.acquire(blocking=False):
        raise PromoteBlocked("Another CV is being tailored; try again in a moment", 409)
    try:
        with db.connect() as conn:
            existing = db.get_tailored(conn, key)
        if existing:  # a repeat click costs nothing
            return describe_result(key, existing, reused=True)
        base, base_text, jd, summary = _context(key)
        if not confirm_cost:
            raise PromoteBlocked("Confirm the estimated cost to tailor this CV", 400)
        return _tailor(key, base, base_text, jd, summary)
    finally:
        _promote_lock.release()


def _tailor(key: str, base: BaseCV, base_text: str, jd: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    message = build_user_message(base, jd, summary["title"], summary["company"])
    system = load_prompt()
    expected = min(max(int(len(json.dumps([e.bullets for e in base.experience])) / 3.5) + 200, 600), 6000)
    extra_skills = load_profile().scoring_skills

    tailored = _ask(key, system, message, expected)
    report = validator.validate(base, base_text, tailored, extra_skills)
    attempts = 1
    if not report.passed:  # one retry with the violations listed
        retry_message = (
            f"{message}\n\n<problems>\nYour previous answer broke these rules:\n{validator.describe(report)}\n"
            "Answer again. Use only facts, terms, and numbers that are written in the CV.\n</problems>"
        )
        tailored = _ask(key, system, retry_message, expected)
        report = validator.validate(base, base_text, tailored, extra_skills)
        attempts = 2

    merged = merge(base, tailored)
    docx_path, pdf_path = export.export_cv(merged, key, config.DATA_DIR)
    validation = {**report.to_dict(), "attempts": attempts}
    with db.connect() as conn:
        db.save_tailored(conn, key, merged, str(docx_path), str(pdf_path) if pdf_path else None, validation)
        stored = db.get_tailored(conn, key)
    status = "passed validation" if report.passed else f"FAILED validation ({len(report.violations)} problems)"
    db.log_activity("cv-adapter", f"Tailored CV for {summary['title']} at {summary['company']}: {status}",
                    "completed" if report.passed else "failed", llm.model_name())
    assert stored is not None
    return describe_result(key, stored, reused=False)


def _ask(key: str, system: str, user: str, expected_output_tokens: int) -> TailoredCV:
    return llm.structured_call(
        purpose="Tailor CV", system=system, user=user, schema=TailoredCV,
        expected_output_tokens=expected_output_tokens, job_key=key,
    )


def describe_result(key: str, stored: dict[str, Any], reused: bool) -> dict[str, Any]:
    validation = stored["validation"]
    warning = None
    if not validation.get("passed", False):
        warning = ("This tailored CV did not pass validation: it may mention skills, numbers, or terms that are not "
                   "in your base CV. Review it carefully before using it.")
    return {
        "key": key, "reused": reused, "validation": validation, "warning": warning, "cv": stored["cv"],
        "has_pdf": bool(stored["pdf_path"] and Path(stored["pdf_path"]).exists()),
    }


# --- downloads ----------------------------------------------------------------------------------------------

def export_file(key: str, kind: str) -> tuple[Path, str]:
    """Path and download name of a tailored CV file ("docx" or "pdf"), regenerating it if it went missing.

    Raises ValueError for an invalid key or a stored path outside the tailored folder, and LookupError
    if there is no tailored CV (or no PDF could be made).
    """
    if not export.KEY_RE.match(key) or kind not in ("docx", "pdf"):
        raise ValueError("invalid request")
    with db.connect() as conn:
        stored = db.get_tailored(conn, key)
    if stored is None:
        raise LookupError("no tailored CV for this job")
    folder = export.tailored_dir(config.DATA_DIR).resolve()
    path_text = stored["docx_path"] if kind == "docx" else stored["pdf_path"]
    path = Path(path_text).resolve() if path_text else folder / f"{key}.{kind}"
    if folder not in path.parents:
        raise ValueError("path is outside the allowed folder")
    if not path.exists():  # regenerate from the stored CV
        docx_path, pdf_path = export.export_cv(stored["cv"], key, config.DATA_DIR)
        with db.connect() as conn:
            db.set_export_paths(conn, key, str(docx_path), str(pdf_path) if pdf_path else None)
        path = docx_path if kind == "docx" else pdf_path
        if path is None:
            raise LookupError("PDF export is not available")
    with db.connect() as conn:
        summary = db.get_job_summary(conn, key) or {}
    stem = "_".join(filter(None, (stored["cv"]["name"], summary.get("company", ""), summary.get("title", ""))))
    safe = "".join(ch if ch.isalnum() else "_" for ch in stem)[:100].strip("_") or "cv"
    return path, f"{safe}.{kind}"


def is_busy() -> bool:
    """True while a CV is being tailored."""
    return _promote_lock.locked()
