"""Remote OK API (no key). Terms: link back to the listing (followed link) and name Remote OK."""
from app.profile import Profile
from app.scout.boards import Board
from app.scout.http import FetchError, ScoutClient, get_json
from app.scout.models import Notes, RawListing
from app.scout.text import positive_number, strip_html

NAME = "remoteok"
URL = "https://remoteok.com/api"


def fetch(profile: Profile, boards: dict[str, list[Board]], client: ScoutClient) -> tuple[list[RawListing], Notes]:
    """Fetch the feed (it only covers a recent window)."""
    notes = Notes()
    data = get_json(client, NAME, URL)
    if not isinstance(data, list):
        raise FetchError("unexpected response shape")
    listings = [
        RawListing(
            source=NAME,
            source_id=str(job["id"]),
            title=job["position"],
            company=job.get("company") or "",
            location=job.get("location") or "Remote",
            description=strip_html(job.get("description")),
            url=job["url"],
            created=job.get("date"),
            salary_min=positive_number(job.get("salary_min")),
            salary_max=positive_number(job.get("salary_max")),
        )
        for job in data[1:]  # first element is a legal notice, not a job
        if isinstance(job, dict) and "id" in job and "position" in job
    ]
    return listings, notes
