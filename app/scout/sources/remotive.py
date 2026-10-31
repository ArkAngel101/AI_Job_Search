"""Remotive remote jobs API (no key).

Terms: fetch only a few times a day (advice: at most 4 per day; more than 2 per minute is blocked),
and link back to the Remotive posting. So this makes ONE unfiltered request and Scout filters locally.
Forge enforces MIN_SCAN_GAP_SECONDS between fetches.
"""
from app.profile import Profile
from app.scout.boards import Board
from app.scout.http import ScoutClient, get_json
from app.scout.models import Notes, RawListing
from app.scout.text import strip_html

NAME = "remotive"
URL = "https://remotive.com/api/remote-jobs"
MIN_SCAN_GAP_SECONDS = 6 * 3600  # 4 requests per day


def fetch(profile: Profile, boards: dict[str, list[Board]], client: ScoutClient) -> tuple[list[RawListing], Notes]:
    """Fetch the full Remotive feed once."""
    notes = Notes()
    data = get_json(client, NAME, URL)
    listings = [
        RawListing(
            source=NAME,
            source_id=str(job["id"]),
            title=job["title"],
            company=job.get("company_name") or "",
            location=job.get("candidate_required_location") or "Remote",
            description=strip_html(job.get("description")),
            url=job["url"],
            created=job.get("publication_date"),
            contract_type=job.get("job_type") or None,
        )
        for job in data["jobs"]
    ]
    return listings, notes
