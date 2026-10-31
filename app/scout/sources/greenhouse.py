"""Greenhouse Job Board API (public, no key). One call per company board."""
import html

from app.profile import Profile
from app.scout.boards import Board
from app.scout.http import FetchError, ScoutClient, get_json
from app.scout.models import Notes, RawListing
from app.scout.sources._ats import run_boards
from app.scout.text import strip_html

NAME = "greenhouse"
URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


def fetch_board(board: Board, client: ScoutClient) -> list[RawListing]:
    data = get_json(client, NAME, URL.format(slug=board.slug), params={"content": "true"})
    jobs = data["jobs"]
    total = (data.get("meta") or {}).get("total")
    if total is not None and total != len(jobs):
        raise FetchError("incomplete response")
    return [
        RawListing(
            source=NAME,
            source_id=str(job["id"]),
            title=job["title"],
            company=board.name,
            location=(job.get("location") or {}).get("name") or "",
            # Greenhouse returns the description as entity-encoded HTML.
            description=strip_html(html.unescape(job.get("content") or "")),
            url=job["absolute_url"],
            created=job.get("updated_at"),
        )
        for job in jobs
    ]


def fetch(profile: Profile, boards: dict[str, list[Board]], client: ScoutClient) -> tuple[list[RawListing], Notes]:
    """Fetch all listed Greenhouse boards."""
    return run_boards(NAME, boards.get(NAME, []), client, fetch_board)
