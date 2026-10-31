"""Recognize job URLs from the supported ATS platforms and fetch them through their official APIs."""
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.scout.boards import SLUG_RE, Board
from app.scout.http import FetchError, ScoutClient
from app.scout.models import RawListing
from app.scout.sources import ashby, greenhouse, lever, workable

_FETCHERS = {
    "greenhouse": greenhouse.fetch_board, "lever": lever.fetch_board,
    "ashby": ashby.fetch_board, "workable": workable.fetch_board,
}
# host -> (ATS, path pattern with slug and id groups)
_PATTERNS = {
    "boards.greenhouse.io": ("greenhouse", r"^/(?P<slug>[^/]+)/jobs/(?P<id>\d+)/?$"),
    "job-boards.greenhouse.io": ("greenhouse", r"^/(?P<slug>[^/]+)/jobs/(?P<id>\d+)/?$"),
    "jobs.lever.co": ("lever", r"^/(?P<slug>[^/]+)/(?P<id>[0-9a-fA-F-]{8,})/?$"),
    "jobs.ashbyhq.com": ("ashby", r"^/(?P<slug>[^/]+)/(?P<id>[0-9a-fA-F-]{8,})/?$"),
    "apply.workable.com": ("workable", r"^/(?P<slug>[^/]+)/j/(?P<id>[0-9A-Za-z]+)/?$"),
}


@dataclass(frozen=True)
class AtsRef:
    """A job on a company board."""

    ats: str
    slug: str
    job_id: str


def parse_ats_url(url: str) -> AtsRef | None:
    """The ATS, board slug, and job id for a recognized job URL, else None."""
    parts = urlsplit(url.strip())
    spec = _PATTERNS.get((parts.hostname or "").lower())
    if parts.scheme not in ("http", "https") or spec is None:
        return None
    match = re.match(spec[1], parts.path)
    if not match or not SLUG_RE.match(match["slug"]):
        return None
    return AtsRef(ats=spec[0], slug=match["slug"], job_id=match["id"])


def fetch_ats_job(ref: AtsRef, client: ScoutClient, company: str | None = None) -> RawListing | None:
    """Fetch the board through the official API and return the one job, or None if it is not listed.

    Raises FetchError when the board itself cannot be fetched (404 means an unknown board slug).
    """
    listings = _FETCHERS[ref.ats](Board(slug=ref.slug, name=company or ref.slug), client)
    return next((item for item in listings if item.source_id.lower() == ref.job_id.lower()), None)


__all__ = ["AtsRef", "FetchError", "fetch_ats_job", "parse_ats_url"]
