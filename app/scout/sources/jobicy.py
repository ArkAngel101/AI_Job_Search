"""Jobicy remote jobs API (no key).

Terms: no automated polling more than once per hour (Forge enforces MIN_SCAN_GAP_SECONDS), honor
Retry-After on 429, keep the canonical job URL and credit Jobicy. Covers the last 7 days.
"""
from app.profile import Profile
from app.scout.boards import Board
from app.scout.http import FetchError, ScoutClient, get_json
from app.scout.models import Notes, RawListing
from app.scout.text import positive_number, strip_html

NAME = "jobicy"
URL = "https://jobicy.com/api/jobs"
MIN_SCAN_GAP_SECONDS = 3600
PAGE_SIZE = 100


def _salary(job: dict, key: str) -> float | None:
    """Only yearly figures are comparable; hourly or monthly numbers are dropped."""
    if str(job.get("salaryPeriod", "")).lower() not in ("yearly", "annual", "year"):
        return None
    return positive_number(job.get(key))


def fetch(profile: Profile, boards: dict[str, list[Board]], client: ScoutClient) -> tuple[list[RawListing], Notes]:
    """Follow the cursor for up to `pages` pages of US-eligible remote jobs."""
    notes = Notes()
    listings: list[RawListing] = []
    cursor: str | None = None
    for _ in range(profile.pages):
        params: dict[str, str | int] = {"count": PAGE_SIZE, "geo": "usa"}
        if cursor:
            params["cursor"] = cursor
        try:
            data = get_json(client, NAME, URL, params=params)
            jobs = data["jobs"]
        except FetchError as exc:
            if listings:
                notes.append(f"jobicy: later page failed ({exc}), kept earlier pages")
                break
            raise
        for job in jobs:
            listings.append(
                RawListing(
                    source=NAME,
                    source_id=str(job["id"]),
                    title=job["jobTitle"],
                    company=job.get("companyName") or "",
                    location=f"Remote, {job.get('jobGeo') or 'Anywhere'}",
                    description=strip_html(job.get("jobDescription")),
                    url=job["url"],
                    created=job.get("pubDate"),
                    salary_min=_salary(job, "salaryMin"),
                    salary_max=_salary(job, "salaryMax"),
                    contract_type=", ".join(job.get("jobType") or []) or None,
                )
            )
        cursor = data.get("nextCursor")
        if not cursor:
            break
    return listings, notes
