"""Lever Postings API (public, no key). One call per company; at least 1 second between calls."""
from app.profile import Profile
from app.scout.boards import Board
from app.scout.http import FetchError, ScoutClient, get_json
from app.scout.models import Notes, RawListing
from app.scout.sources._ats import run_boards
from app.scout.text import epoch_ms_to_iso, join_location, positive_number, strip_html

NAME = "lever"
URL = "https://api.lever.co/v0/postings/{slug}"
MIN_INTERVAL = 1.0


def _description(posting: dict) -> str:
    parts = [posting.get("descriptionPlain") or strip_html(posting.get("description"))]
    for section in posting.get("lists") or []:
        parts.append(f"{section.get('text', '')}\n{strip_html(section.get('content'))}".strip())
    parts.append(posting.get("additionalPlain") or strip_html(posting.get("additional")))
    return "\n\n".join(p for p in parts if p)


def fetch_board(board: Board, client: ScoutClient) -> list[RawListing]:
    data = get_json(client, NAME, URL.format(slug=board.slug), params={"mode": "json"}, min_interval=MIN_INTERVAL)
    if not isinstance(data, list):
        raise FetchError("unexpected response shape")
    listings = []
    for posting in data:
        salary = posting.get("salaryRange") or {}
        categories = posting.get("categories") or {}
        listings.append(
            RawListing(
                source=NAME,
                source_id=str(posting["id"]),
                title=posting["text"],
                company=board.name,
                location=join_location(categories.get("location"), remote=posting.get("workplaceType") == "remote"),
                description=_description(posting),
                url=posting["hostedUrl"],
                created=epoch_ms_to_iso(posting.get("createdAt")),  # not documented; present on live data
                salary_min=positive_number(salary.get("min")),
                salary_max=positive_number(salary.get("max")),
                contract_type=categories.get("commitment"),
            )
        )
    return listings


def fetch(profile: Profile, boards: dict[str, list[Board]], client: ScoutClient) -> tuple[list[RawListing], Notes]:
    """Fetch all listed Lever boards."""
    return run_boards(NAME, boards.get(NAME, []), client, fetch_board)
