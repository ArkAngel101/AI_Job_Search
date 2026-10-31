"""Add a job by pasted URL or pasted text (Forge calls this; Job Reader then reads it)."""
import hashlib
from typing import Any
from urllib.parse import urlsplit

from app import db
from app.reader import reader
from app.reader.ats_urls import AtsRef, fetch_ats_job, parse_ats_url
from app.reader.page import JSON_LD_MIN_CHARS, looks_unreadable, page_meta, page_text, parse_job_posting
from app.reader.reader import Prefetched
from app.reader.safe_fetch import PageFetchError, UnsafeURL
from app.scout.boards import load_boards
from app.scout.dedup import canonical_url
from app.scout.http import FetchError, ScoutClient
from app.scout.models import RawListing

UNREADABLE = (
    "Couldn't read that page. Many sites, such as LinkedIn and Indeed, block automated reading. "
    "Please paste the job description text instead."
)
MIN_TEXT_CHARS = 40


class AddJobError(Exception):
    """The job could not be added. `needs_text` tells the UI to ask for the description text."""

    def __init__(self, message: str, *, status: int = 422, needs_text: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.needs_text = needs_text


def make_client() -> ScoutClient:
    """HTTP client for ATS API calls (tests replace this)."""
    return ScoutClient()


def add_job(
    url: str | None = None, text: str | None = None, title: str | None = None,
    company: str | None = None, location: str | None = None,
) -> dict[str, Any]:
    """Add a job from pasted text or a URL, read it, and return its summary. Raises AddJobError."""
    url, text = (url or "").strip(), (text or "").strip()
    if not url and not text:
        raise AddJobError("Provide a job URL or the job description text", status=422)
    if url and urlsplit(url).scheme.lower() not in ("http", "https"):
        raise AddJobError("Only http and https links are accepted", status=400)
    if text:  # pasted text wins; a URL given with it is kept only as a link to the posting
        if len(text) < MIN_TEXT_CHARS:
            raise AddJobError("The pasted text is too short to be a job description")
        item = RawListing(
            source="pasted", source_id=hashlib.sha1(text.encode()).hexdigest()[:12],
            title=(title or _first_line(text)).strip(), company=(company or "Unknown").strip(),
            location=(location or "").strip(), description=text, url=url,
        )
        return _store_and_read(item, Prefetched(text, "pasted-text"))
    ref = parse_ats_url(url)
    return _add_ats(ref) if ref else _add_web(url)


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "Untitled job")[:120]


def _add_ats(ref: AtsRef) -> dict[str, Any]:
    """A job on a known ATS: fetch the whole board through its official API and pick the job."""
    boards = load_boards()
    known = next((b for b in boards.get(ref.ats, []) if b.slug.lower() == ref.slug.lower()), None)
    company = known.name if known else ref.slug.replace("-", " ").replace("_", " ").title()
    with make_client() as client:
        try:
            item = fetch_ats_job(ref, client, company=company)
        except FetchError as exc:
            if exc.status == 404:
                raise AddJobError(
                    f"The {ref.ats} board '{ref.slug}' was not found. Check the link, or paste the description text.",
                    needs_text=True,
                ) from None
            raise AddJobError(f"Couldn't reach {ref.ats} ({exc}). Try again, or paste the description text.",
                              needs_text=True) from None
    if item is None:
        raise AddJobError("That job isn't listed on the board any more (it may be closed). "
                          "Paste the description text instead.", needs_text=True)
    item.board = f"{ref.ats}/{known.slug}" if known else None
    offer = None if known else {"ats": ref.ats, "slug": ref.slug}
    return _store_and_read(item, None, offer_add_board=offer)


def _add_web(url: str) -> dict[str, Any]:
    """Any other URL: JSON-LD JobPosting first, then the page text. Never bypasses blocks."""
    try:
        page = reader.fetch_page(url)
    except UnsafeURL as exc:
        raise AddJobError(str(exc), status=400) from None
    except PageFetchError:
        raise AddJobError(UNREADABLE, needs_text=True) from None
    canonical = canonical_url(page.url)
    source_id = hashlib.sha1(canonical.encode()).hexdigest()[:12]
    posting = parse_job_posting(page.text) if page.status == 200 else None
    if posting and len(posting["description"]) >= JSON_LD_MIN_CHARS:
        hints = {k: posting[k] for k in ("salary", "remote") if posting.get(k)}
        item = RawListing(
            source="web", source_id=source_id, title=posting["title"] or _host(page.url),
            company=posting["company"] or _host(page.url), location=posting["location"],
            description=posting["description"], url=page.url, created=posting["created"],
            contract_type=posting["contract_type"],
        )
        return _store_and_read(item, Prefetched(posting["description"], "json-ld", hints=hints))
    text = page_text(page.text)
    if looks_unreadable(text, page.status):
        raise AddJobError(UNREADABLE, needs_text=True)
    meta = page_meta(page.text)
    item = RawListing(
        source="web", source_id=source_id, title=meta["title"] or _host(page.url),
        company=meta["site"] or _host(page.url), location="", description=text, url=page.url,
    )
    return _store_and_read(item, Prefetched(text, "page-text"))


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "Unknown").removeprefix("www.")


def _store_and_read(
    item: RawListing, prefetched: Prefetched | None, offer_add_board: dict[str, str] | None = None
) -> dict[str, Any]:
    with db.connect() as conn:
        key, created = db.insert_added_job(conn, item, item.board)
    jd = reader.read_job(key, prefetched=prefetched)  # a job already read comes back from cache
    with db.connect() as conn:
        summary = db.get_job_summary(conn, key)
    return {
        "key": key, "created": created, "partial": jd["partial"], "read_via": jd["read_via"],
        "offer_add_board": offer_add_board if created else None, "job": summary,
    }
