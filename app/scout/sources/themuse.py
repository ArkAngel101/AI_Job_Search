"""The Muse public jobs API (no key; 500 requests per hour). Terms: link back to themuse.com."""
from app.profile import Profile
from app.scout.boards import Board
from app.scout.http import FetchError, ScoutClient, get_json
from app.scout.models import Notes, RawListing
from app.scout.text import strip_html

NAME = "themuse"
URL = "https://www.themuse.com/api/public/jobs"
# Category names taken from The Muse's docs examples; the full list was not readable.
CATEGORIES = ("Data Science", "Software Engineering")


def fetch(profile: Profile, boards: dict[str, list[Board]], client: ScoutClient) -> tuple[list[RawListing], Notes]:
    """Newest `pages` pages for each category."""
    notes = Notes()
    found: dict[str, RawListing] = {}
    for category in CATEGORIES:
        for page in range(1, profile.pages + 1):
            try:
                data = get_json(client, NAME, URL, params={"page": page, "category": category, "descending": "true"})
                results = data["results"]
            except FetchError as exc:
                notes.append(f"themuse: '{category}' page {page} failed ({exc})")
                break
            except (KeyError, TypeError):
                notes.append(f"themuse: '{category}' page {page} unexpected response shape")
                break
            if not results:
                break
            for job in results:
                found.setdefault(
                    str(job["id"]),
                    RawListing(
                        source=NAME,
                        source_id=str(job["id"]),
                        title=job["name"],
                        company=(job.get("company") or {}).get("name") or "",
                        location="; ".join(loc["name"] for loc in job.get("locations") or [] if loc.get("name")),
                        description=strip_html(job.get("contents")),
                        url=(job.get("refs") or {})["landing_page"],
                        created=job.get("publication_date"),
                    ),
                )
    return list(found.values()), notes
