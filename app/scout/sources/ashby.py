"""Ashby public job posting API (no key). One call per company board."""
from app.profile import Profile
from app.scout.boards import Board
from app.scout.http import ScoutClient, get_json
from app.scout.models import Notes, RawListing
from app.scout.sources._ats import run_boards
from app.scout.text import join_location, positive_number

NAME = "ashby"
URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def _salary(job: dict) -> tuple[float | None, float | None]:
    """Min/max of the first Salary component, if compensation was returned."""
    components = (job.get("compensation") or {}).get("summaryComponents") or []
    for comp in components:
        if comp.get("compensationType") == "Salary":
            return positive_number(comp.get("minValue")), positive_number(comp.get("maxValue"))
    return None, None


def fetch_board(board: Board, client: ScoutClient) -> list[RawListing]:
    data = get_json(client, NAME, URL.format(slug=board.slug), params={"includeCompensation": "true"})
    listings = []
    for job in data["jobs"]:
        if job.get("isListed") is False:
            continue  # unlisted: reachable only by direct link
        low, high = _salary(job)
        listings.append(
            RawListing(
                source=NAME,
                # `id` is not on the docs page; fall back to the last path segment of jobUrl.
                source_id=str(job.get("id") or job["jobUrl"].rstrip("/").rsplit("/", 1)[-1]),
                title=job["title"],
                company=board.name,
                location=join_location(job.get("location"), remote=job.get("workplaceType") == "Remote"),
                description=job.get("descriptionPlain") or "",
                url=job["jobUrl"],
                created=job.get("publishedAt"),
                salary_min=low,
                salary_max=high,
            )
        )
    return listings


def fetch(profile: Profile, boards: dict[str, list[Board]], client: ScoutClient) -> tuple[list[RawListing], Notes]:
    """Fetch all listed Ashby boards."""
    return run_boards(NAME, boards.get(NAME, []), client, fetch_board)
