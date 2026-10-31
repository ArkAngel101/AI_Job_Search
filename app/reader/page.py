"""Read a job posting out of an HTML page: JSON-LD first, cleaned page text as the fallback."""
import html
import json
from typing import Any

from bs4 import BeautifulSoup

from app.scout.text import strip_html

BLOCKED_STATUSES = {401, 403, 407, 429, 451, 999}
THIN_CHARS = 400  # a page with less text than this is not treated as a posting
JSON_LD_MIN_CHARS = 200  # shortest JSON-LD description worth using
BLOCK_MARKERS = (
    "sign in to", "log in to", "login to continue", "captcha", "verify you are human", "are you a robot",
    "enable javascript", "access denied", "unusual traffic", "please enable cookies", "checking your browser",
    "just a moment", "create an account to",
)
_NOISE_TAGS = ["script", "style", "nav", "header", "footer", "noscript", "form", "svg", "aside"]


def _walk(node: Any) -> list[dict[str, Any]]:
    """Flatten JSON-LD (lists and @graph) into a list of dict nodes."""
    if isinstance(node, list):
        return [item for child in node for item in _walk(child)]
    if isinstance(node, dict):
        found = [node]
        if "@graph" in node:
            found += _walk(node["@graph"])
        return found
    return []


def _is_job_posting(node: dict[str, Any]) -> bool:
    kind = node.get("@type")
    return kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind)


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("@value") or "")
    return str(value).strip() if value else ""


def _location(posting: dict[str, Any]) -> str:
    places = posting.get("jobLocation") or []
    places = places if isinstance(places, list) else [places]
    names = []
    for place in places:
        address = place.get("address", {}) if isinstance(place, dict) else {}
        if isinstance(address, str):
            names.append(address)
        elif isinstance(address, dict):
            parts = [_text(address.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry")]
            names.append(", ".join(p for p in parts if p))
    return "; ".join(n for n in names if n)


def _salary(posting: dict[str, Any]) -> dict[str, Any] | None:
    base = posting.get("baseSalary")
    if not isinstance(base, dict):
        return None
    value = base.get("value", {})
    value = value if isinstance(value, dict) else {"value": value}
    low, high = value.get("minValue", value.get("value")), value.get("maxValue", value.get("value"))
    try:
        low_n, high_n = (float(low) if low is not None else None), (float(high) if high is not None else None)
    except (TypeError, ValueError):
        return None
    if low_n is None and high_n is None:
        return None
    return {"min": low_n, "max": high_n, "currency": _text(base.get("currency")) or None,
            "period": _text(value.get("unitText")).lower() or None, "source": "json-ld"}


def parse_job_posting(page_html: str) -> dict[str, Any] | None:
    """The first JSON-LD JobPosting on the page as a dict, or None."""
    soup = BeautifulSoup(page_html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text(), strict=False)
        except ValueError:
            continue
        for node in _walk(data):
            if not _is_job_posting(node):
                continue
            description = str(node.get("description") or "")
            if "<" not in description and "&lt;" in description:
                description = html.unescape(description)
            return {
                "title": _text(node.get("title")),
                "company": _text(node.get("hiringOrganization")),
                "location": _location(node),
                "remote": _text(node.get("jobLocationType")).upper() == "TELECOMMUTE",
                "salary": _salary(node),
                "description": strip_html(description),
                "contract_type": _text(node.get("employmentType")) or None,
                "created": _text(node.get("datePosted")) or None,
            }
    return None


def page_text(page_html: str) -> str:
    """Readable text of the page's main content (navigation, scripts, and forms removed)."""
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    return strip_html(str(main))


def looks_unreadable(text: str, status: int) -> bool:
    """True for blocked pages, login walls, bot checks, and pages with too little text to be a posting."""
    if status != 200 or status in BLOCKED_STATUSES:
        return True
    if len(text) < THIN_CHARS:
        return True
    return len(text) < 2500 and any(marker in text.lower() for marker in BLOCK_MARKERS)


def page_meta(page_html: str) -> dict[str, str]:
    """Best-effort title (h1 or <title>) and site name (og:site_name) of a page."""
    soup = BeautifulSoup(page_html, "html.parser")
    heading = soup.find("h1")
    title = (heading.get_text(" ", strip=True) if heading else "") or (soup.title.get_text(" ", strip=True) if soup.title else "")
    site = soup.find("meta", property="og:site_name")
    return {"title": title[:200], "site": str(site.get("content", ""))[:100] if site else ""}
