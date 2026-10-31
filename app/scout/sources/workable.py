"""Workable public account widget feed (no key; not in Workable's official API docs).

Endpoint and fields were checked against a live response, because Workable's help article lists
only the www.workable.com form, which redirects here. Pace requests about 3 seconds apart.
"""
from app.profile import Profile
from app.scout.boards import Board
from app.scout.http import ScoutClient, get_json
from app.scout.models import Notes, RawListing
from app.scout.sources._ats import run_boards
from app.scout.text import join_location, strip_html

NAME = "workable"
URL = "https://apply.workable.com/api/v1/widget/accounts/{slug}"
MIN_INTERVAL = 3.0


def _fetch_board(board: Board, client: ScoutClient) -> list[RawListing]:
    data = get_json(client, NAME, URL.format(slug=board.slug), params={"details": "true"}, min_interval=MIN_INTERVAL)
    company = data.get("name") or board.name
    return [
        RawListing(
            source=NAME,
            source_id=str(job["shortcode"]),
            title=job["title"],
            company=company,
            location=join_location(
                job.get("city"), job.get("state"), job.get("country"), remote=bool(job.get("telecommuting"))
            ),
            description=strip_html(job.get("description")),
            url=job["url"],
            created=job.get("published_on") or job.get("created_at"),
            contract_type=job.get("employment_type"),
        )
        for job in data["jobs"]
    ]


def fetch(profile: Profile, boards: dict[str, list[Board]], client: ScoutClient) -> tuple[list[RawListing], Notes]:
    """Fetch all listed Workable accounts."""
    return run_boards(NAME, boards.get(NAME, []), client, _fetch_board)
