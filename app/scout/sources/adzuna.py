"""Adzuna US job search (free key: ADZUNA_APP_ID and ADZUNA_APP_KEY).

Error notes never include the request URL, because it carries the app key.
"""
import os

from app.profile import Profile
from app.scout.boards import Board
from app.scout.http import FetchError, ScoutClient, get_json
from app.scout.models import Notes, RawListing
from app.scout.text import positive_number, scrub_url, strip_html

NAME = "adzuna"
REQUIRES_ENV = ("ADZUNA_APP_ID", "ADZUNA_APP_KEY")  # a free key from developer.adzuna.com
URL = "https://api.adzuna.com/v1/api/jobs/us/search/{page}"
RESULTS_PER_PAGE = 20


def _search(client: ScoutClient, creds: dict[str, str], what: str, page: int, by_date: bool) -> list[dict]:
    params = {**creds, "results_per_page": RESULTS_PER_PAGE, "what": what, "content-type": "application/json"}
    if by_date:
        params["sort_by"] = "date"
    return get_json(client, NAME, URL.format(page=page), params=params).get("results") or []


def _to_listing(item: dict) -> RawListing:
    return RawListing(
        source=NAME,
        source_id=str(item["id"]),
        title=item["title"],
        company=(item.get("company") or {}).get("display_name") or "",
        location=(item.get("location") or {}).get("display_name") or "",
        description=strip_html(item.get("description")),  # Adzuna returns only a snippet
        url=scrub_url(item["redirect_url"], [os.environ.get(name, "").strip() for name in REQUIRES_ENV]),
        created=item.get("created"),
        salary_min=positive_number(item.get("salary_min")),
        salary_max=positive_number(item.get("salary_max")),
        contract_type=item.get("contract_type"),
    )


def fetch(profile: Profile, boards: dict[str, list[Board]], client: ScoutClient) -> tuple[list[RawListing], Notes]:
    """For each keyword: newest pages 1..pages plus relevance page 1. Skipped without a key."""
    notes = Notes()
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        notes.append("adzuna: skipped, ADZUNA_APP_ID / ADZUNA_APP_KEY not set")
        return [], notes
    creds = {"app_id": app_id, "app_key": app_key}
    found: dict[str, RawListing] = {}
    for keyword in profile.keywords:
        try:
            # Adzuna ANDs all words: drop trailing words until the search returns something.
            words, results = keyword.split(), []
            while words:
                what = " ".join(words)
                results = _search(client, creds, what, 1, by_date=True)
                if results:
                    break
                words.pop()
            if not results:
                notes.append(f"adzuna: no results for '{keyword}'")
                continue
            if what != keyword:
                notes.append(f"adzuna: '{keyword}' had no results, searched '{what}' instead")
            batches = [results]
            batches += [_search(client, creds, what, page, by_date=True) for page in range(2, profile.pages + 1)]
            batches.append(_search(client, creds, what, 1, by_date=False))
        except FetchError as exc:
            notes.append(f"adzuna: '{keyword}' failed ({exc})")
            if exc.status in (401, 403):
                notes.append("adzuna: credentials rejected, stopping this source")
                break
            continue
        for item in (item for batch in batches for item in batch):
            found.setdefault(str(item["id"]), _to_listing(item))
    return list(found.values()), notes
