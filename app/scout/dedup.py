"""Job identity: canonical URLs, normalized keys, and collapsing duplicate listings.

Two listings are the same job when they share, in this order of strength:
  1. (source, source_id)
  2. canonical URL (tracking parameters stripped)
  3. normalized title + company + location
"""
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.scout.models import RawListing

# Sources that return a company's own posting are preferred over aggregators.
ATS_SOURCES = ("greenhouse", "lever", "ashby", "workable")

TRACKING_PARAMS = {"gh_src", "lever-source", "lever-origin", "ref", "source", "gclid", "fbclid"}
COMPANY_SUFFIXES = {"inc", "llc", "ltd", "corp", "co", "group", "gmbh", "ag", "se", "technologies"}
COUNTRY_TOKENS = {"united states", "united states of america", "usa", "us", "america"}
REMOTE_WORDS = re.compile(r"\b(remote|anywhere|work from home|wfh|telecommute|distributed)\b")

US_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca", "colorado": "co",
    "connecticut": "ct", "delaware": "de", "district of columbia": "dc", "florida": "fl", "georgia": "ga",
    "hawaii": "hi", "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md", "massachusetts": "ma",
    "michigan": "mi", "minnesota": "mn", "mississippi": "ms", "missouri": "mo", "montana": "mt",
    "nebraska": "ne", "nevada": "nv", "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm",
    "new york": "ny", "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
    "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
}
_STATE_RE = re.compile(r"\b(" + "|".join(sorted(US_STATES, key=len, reverse=True)) + r")\b")
_COUNTRY_RE = re.compile(r"\b(" + "|".join(sorted(COUNTRY_TOKENS, key=len, reverse=True)) + r")\b")

# "(m/f/d)", "(w/m/x)", "m/f/d", "(all genders)", "(gn)"
_GENDER_TAG_RE = re.compile(
    r"\(\s*[mfwdx](?:\s*[/|,]\s*[mfwdx])+\s*\)"
    r"|\b[mfwdx](?:/[mfwdx]){1,2}\b"
    r"|\(\s*(?:all genders|gn)\s*\)",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    """Lowercase, drop accents and punctuation, collapse spaces."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ").replace(".", "")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]|_", " ", text)).strip()


def normalize_title(title: str) -> str:
    """Normalize a job title, removing gender/format tags like (m/f/d)."""
    return _clean(_GENDER_TAG_RE.sub(" ", title or ""))


def normalize_company(name: str) -> str:
    """Normalize a company name, dropping trailing suffixes such as Inc, LLC, GmbH."""
    tokens = _clean(name or "").split()
    while len(tokens) > 1 and tokens[-1] in COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_location(location: str) -> str:
    """Normalize a location. Anything remote becomes the single value 'remote'.

    US state names become abbreviations and country words are dropped, so
    'Boston, Massachusetts' and 'Boston, MA, United States' compare equal.
    """
    text = _clean(location or "")
    if REMOTE_WORDS.search(text):
        return "remote"
    text = _STATE_RE.sub(lambda m: US_STATES[m.group(1)], text)
    return re.sub(r"\s+", " ", _COUNTRY_RE.sub(" ", text)).strip()


def fuzzy_key(title: str, company: str, location: str) -> str | None:
    """Normalized title|company|location, or None when title or company is missing."""
    norm_title, norm_company = normalize_title(title), normalize_company(company)
    if not norm_title or not norm_company:
        return None
    return f"{norm_title}|{norm_company}|{normalize_location(location)}"


def canonical_url(url: str) -> str:
    """URL with tracking parameters, tracking fragments, and trailing slashes removed; host lowercased."""
    parts = urlsplit((url or "").strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower().removesuffix("[]") not in TRACKING_PARAMS
    ]
    # Keep only fragments that are client-side routes (#/jobs/1); drop anchors like #app or #.abc123.
    fragment = parts.fragment if parts.fragment.startswith(("/", "!/")) else ""
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), fragment))


def job_key(source: str, source_id: str) -> str:
    """Stable, filename-safe primary key for a job, from its first-seen source identity."""
    return hashlib.sha1(f"{source}\x1f{source_id}".encode()).hexdigest()[:16]


Alias = tuple[str, str]  # (kind, value) with kind in "id", "url", "fuzzy"


def aliases(listing: RawListing) -> list[Alias]:
    """Identity aliases of a listing, strongest first."""
    result: list[Alias] = [("id", f"{listing.source}:{listing.source_id}")]
    if listing.url:
        result.append(("url", canonical_url(listing.url)))
    fuzzy = fuzzy_key(listing.title, listing.company, listing.location)
    if fuzzy:
        result.append(("fuzzy", fuzzy))
    return result


def is_better(new_source: str, new_description: str, old_source: str, old_description: str) -> bool:
    """True if the new listing should replace the stored one: ATS first, then the fuller description."""
    new_rank = (new_source in ATS_SOURCES, len(new_description or ""))
    old_rank = (old_source in ATS_SOURCES, len(old_description or ""))
    return new_rank > old_rank


@dataclass
class MergedListing:
    """One job after collapsing duplicates from several sources."""

    listing: RawListing  # the preferred listing, with blanks filled from the others
    sources: list[str]  # every source that listed it, preferred source first
    aliases: list[Alias] = field(default_factory=list)  # aliases of all members
    board: str | None = None

    @property
    def key(self) -> str:
        """Primary key used when this job is first stored."""
        return job_key(self.listing.source, self.listing.source_id)


def collapse(listings: list[RawListing]) -> list[MergedListing]:
    """Merge listings that match on any level into one job each, keeping the best listing."""
    parent = list(range(len(listings)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_with: dict[Alias, int] = {}
    member_aliases = [aliases(item) for item in listings]
    for index, alias_list in enumerate(member_aliases):
        for alias in alias_list:
            other = first_with.setdefault(alias, index)
            if other != index:
                parent[find(index)] = find(other)

    groups: dict[int, list[int]] = {}
    for index in range(len(listings)):
        groups.setdefault(find(index), []).append(index)

    merged = []
    for members in groups.values():
        ranked = sorted(
            members,
            key=lambda i: (listings[i].source not in ATS_SOURCES, -len(listings[i].description or ""), i),
        )
        best = _fill_blanks(listings[ranked[0]], [listings[i] for i in ranked[1:]])
        merged.append(
            MergedListing(
                listing=best,
                sources=list(dict.fromkeys(listings[i].source for i in ranked)),
                aliases=list(dict.fromkeys(a for i in members for a in member_aliases[i])),
                board=next((listings[i].board for i in ranked if listings[i].board), None),
            )
        )
    return merged


def _fill_blanks(best: RawListing, others: list[RawListing]) -> RawListing:
    """Copy of `best` with missing salary, date, or contract type taken from the other listings."""
    result = RawListing(**{name: getattr(best, name) for name in RawListing.__slots__})
    for other in others:
        for name in ("created", "contract_type"):
            if getattr(result, name) is None and getattr(other, name) is not None:
                setattr(result, name, getattr(other, name))
        # Take the salary range as a pair so min and max always come from the same listing.
        if result.salary_min is None and result.salary_max is None:
            result.salary_min, result.salary_max = other.salary_min, other.salary_max
    return result
