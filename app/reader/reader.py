"""Job Reader: get a job's full description and extract structured fields. No LLM.

ATS sources already return full descriptions. Listings that carry a full description are used as is;
only thin listings (for example Adzuna snippets) trigger a page fetch, and a blocked or thin page falls
back to the listing's own text, marked `partial`.
"""
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from app import db
from app.profile import load_profile
from app.reader import safe_fetch
from app.reader.extract import extract
from app.reader.page import JSON_LD_MIN_CHARS, looks_unreadable, page_text, parse_job_posting
from app.scout.dedup import ATS_SOURCES
from app.scout.models import RawListing
from app.scout.scoring import score_listing

THIN_LISTING_CHARS = 800  # below this a listing is treated as a snippet and the page is fetched

fetch_page = safe_fetch.fetch_public_page  # module-level so tests can replace it


@dataclass
class Prefetched:
    """A description the caller already has (pasted text, or a page read when the job was added)."""

    text: str
    via: str
    partial: bool = False
    hints: dict[str, Any] = field(default_factory=dict)


def _load_row(key: str) -> sqlite3.Row:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise KeyError(key)
    return row


def _description_for(row: sqlite3.Row) -> tuple[Prefetched, str | None]:
    """Full text for a stored job, plus a note when something fell back."""
    description = row["description"] or ""
    source = row["source"] or ""
    if source in ATS_SOURCES:
        return Prefetched(description, "ats", partial=not description), None
    if source == "pasted":
        return Prefetched(description, "pasted-text"), None
    if len(description) >= THIN_LISTING_CHARS or not row["url"]:
        return Prefetched(description, "listing", partial=len(description) < THIN_LISTING_CHARS), None

    note = None
    try:
        page = fetch_page(row["url"])
    except (safe_fetch.UnsafeURL, safe_fetch.PageFetchError) as exc:
        note = f"page not fetched: {exc}"
    else:
        posting = parse_job_posting(page.text) if page.status == 200 else None
        if posting and len(posting["description"]) >= JSON_LD_MIN_CHARS:
            hints = {k: posting[k] for k in ("salary", "remote") if posting.get(k)}
            return Prefetched(posting["description"], "json-ld", hints=hints), None
        text = page_text(page.text)
        if not looks_unreadable(text, page.status):
            return Prefetched(text, "page-text"), None
        note = "page was blocked, behind a login, or too short; used the listing's own description"
    return Prefetched(description, "listing", partial=True), note


def read_job(key: str, prefetched: Prefetched | None = None) -> dict[str, Any]:
    """Read one job: extract fields, re-score at final mode, mark it read. A job already read is cached."""
    row = _load_row(key)
    if row["jd_json"]:
        return json.loads(row["jd_json"])  # never re-fetched
    profile = load_profile()
    note = None
    if prefetched is None:
        prefetched, note = _description_for(row)
    title, company = row["title"] or "", row["company"] or ""

    jd = extract(
        prefetched.text, title=title, company=company, location=row["location"] or "",
        profile_skills=profile.scoring_skills, hints=prefetched.hints,
    )
    jd.update(read_via=prefetched.via, partial=prefetched.partial, note=note, text=prefetched.text)

    listing = RawListing(
        source=row["source"] or "", source_id=row["source_id"] or "", title=title, company=company,
        location=jd["location"], description=prefetched.text, url=row["url"] or "",
    )
    result = score_listing(listing, profile, "final")
    flags = {**result["flags"], "needs_sponsorship_or_citizenship": jd["flags"]["needs_sponsorship_or_citizenship"]}
    with db.connect() as conn:
        db.save_jd(conn, key, jd, result["score"], {**result["breakdown"], "flags": flags})
    suffix = " (partial)" if prefetched.partial else ""
    db.log_activity("job-reader", f"Read {title} at {company}{suffix}", "completed")
    return jd
