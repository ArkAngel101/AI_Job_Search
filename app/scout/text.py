"""Text helpers: HTML to plain text, number and date cleanup."""
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

_BLOCK_TAGS = ["p", "div", "ul", "ol", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"]


def strip_html(text: str | None) -> str:
    """Convert an HTML fragment to readable plain text (bullets kept, blank lines collapsed)."""
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("- ")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_after("\n")
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in soup.get_text().splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def positive_number(value: object) -> float | None:
    """Return value as a float if it is a positive number, else None (0 means 'not listed')."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value > 0 else None


def epoch_ms_to_iso(value: object) -> str | None:
    """Convert epoch milliseconds to an ISO-8601 UTC string, or None if invalid."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def join_location(*parts: str | None, remote: bool = False) -> str:
    """Join non-empty location parts; append 'Remote' when the posting is remote."""
    items = [p.strip() for p in parts if p and p.strip()]
    if remote and not any("remote" in p.lower() for p in items):
        items.append("Remote")
    return ", ".join(items)


def scrub_url(url: str, secrets: list[str]) -> str:
    """Drop any query parameter whose value contains a secret (Adzuna adds the app id to its links)."""
    secrets = [s for s in secrets if s]
    if not url or not any(s in url for s in secrets):
        return url
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not any(s in v or s in k for s in secrets)]
    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))
    return cleaned if not any(s in cleaned for s in secrets) else ""  # never keep a link that still carries one
