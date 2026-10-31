"""Detect and replace contact details so they never reach the LLM, and put them back afterwards.

Detection runs once per upload; the owner can add or remove items on the CV page. Sanitizing applies
exactly the stored items to the whole text (header, body, and footer alike). City and state alone
(for example "Boston, MA") are NOT treated as an address, so employer locations survive; add one as an
item by hand to hide it.
"""
import re
from dataclasses import dataclass

KINDS = ("name", "email", "phone", "address", "linkedin", "github", "url")
_TOKEN = {"name": "NAME", "email": "EMAIL", "phone": "PHONE", "address": "ADDRESS",
          "linkedin": "LINKEDIN", "github": "GITHUB", "url": "URL"}
_TOKEN_RE = re.compile(r"\[(NAME|EMAIL|PHONE|ADDRESS|LINKEDIN|GITHUB|URL)(?:_(\d+))?\]")
HEADER_LINES = 12  # bare domains (janedoe.dev) are only recognized in the first lines

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PHONE_RES = [
    re.compile(r"(?<![\w.])(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?![\w])"),
    re.compile(r"(?<![\w.])\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}(?![\w])"),
]
_URL_RES = [
    re.compile(r"(?:https?://|www\.)[^\s<>()\[\]\"'|]+", re.IGNORECASE),
    re.compile(r"\b(?:[a-z0-9-]+\.)?(?:linkedin\.com|github\.com|gitlab\.com|bitbucket\.org|kaggle\.com|medium\.com|"
               r"huggingface\.co|twitter\.com|x\.com|scholar\.google\.com|orcid\.org)/[^\s<>()\[\]\"'|]*", re.IGNORECASE),
]
_BARE_DOMAIN_RE = re.compile(
    r"(?<![\w@.])[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|io|dev|net|org|ai|me|co|app|xyz|tech|info|us|page|site)"
    r"(?:/[^\s<>()\[\]\"'|]*)?(?![\w@])", re.IGNORECASE)
_STREET_RE = re.compile(
    r"(?<![\w.-])\d{1,6}\s+(?:(?:\d+(?:st|nd|rd|th)|[A-Za-z][\w.'-]*)\s+){0,4}?"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl|Square|Sq|Parkway|"
    r"Pkwy|Terrace|Ter|Highway|Hwy)\b\.?"
    r"(?:,?\s*(?:Apt|Apartment|Unit|Suite|Ste|Floor|Fl|#)\.?\s*[\w-]+)?"
    r"(?:,\s*[A-Z][A-Za-z.' -]{0,30})?(?:,?\s+[A-Z]{2}\b)?(?:\s+\d{5}(?:-\d{4})?)?",
)
_CITY_STATE_ZIP_RE = re.compile(r"[A-Z][A-Za-z.'-]+(?: [A-Z][A-Za-z.'-]+){0,3},\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?")
_NOT_A_NAME = re.compile(r"\b(resume|résumé|cv|curriculum|vitae|profile|summary|objective|experience|education|"
                         r"skills|projects|contact|developer|engineer|scientist|analyst|manager|intern|student|graduate|designer|"
                         r"consultant|researcher|specialist|architect|director|university|college)\b", re.IGNORECASE)
_NAME_WORD = re.compile(r"^(?:[A-Z][A-Za-z'’-]*\.?|[A-Z]\.?)$")


@dataclass(frozen=True)
class Item:
    """One detected or added contact detail."""

    kind: str
    value: str


def _trim(url: str) -> str:
    return url.rstrip(".,;:!?)")


def _url_kind(url: str) -> str:
    low = url.lower()
    return "linkedin" if "linkedin.com" in low else "github" if "github.com" in low else "url"


def detect_name(text: str) -> str | None:
    """The first of the opening lines that looks like a person's name (2-4 capitalized words)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:6]
    for line in lines:
        words = line.split()
        if not 2 <= len(words) <= 4 or any(ch.isdigit() or ch in "@/|:" for ch in line) or _NOT_A_NAME.search(line):
            continue
        if all(_NAME_WORD.match(w) or w.lower() in {"de", "van", "von", "da", "di", "la", "le"} for w in words):
            return " ".join(words)
    return None


def detect(text: str) -> list[Item]:
    """Find the name, emails, phone numbers, links, and street addresses anywhere in the text.

    The name comes first; everything else is ordered by where it first appears.
    """
    found: list[Item] = []
    seen: set[str] = set()

    def add(kind: str, value: str) -> None:
        value = value.strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            found.append(Item(kind, value))

    name = detect_name(text)
    if name:
        add("name", name)
    for match in _EMAIL_RE.finditer(text):
        add("email", match.group(0))
    work = _EMAIL_RE.sub(" ", text)  # so the host part of an email is not read as a website
    for pattern in _URL_RES:  # each pass blanks what it found, so a link is never found twice
        for match in pattern.finditer(work):
            url = _trim(match.group(0))
            add(_url_kind(url), url)
        work = pattern.sub(" ", work)
    header = "\n".join(work.splitlines()[:HEADER_LINES])
    for match in _BARE_DOMAIN_RE.finditer(header):
        domain = _trim(match.group(0))
        add(_url_kind(domain), domain)
    for pattern in _PHONE_RES:
        for match in pattern.finditer(text):
            add("phone", match.group(0).strip())
    for match in _STREET_RE.finditer(text):
        add("address", match.group(0))
    for match in _CITY_STATE_ZIP_RE.finditer(text):
        if not any(match.group(0).lower() in existing for existing in seen):
            add("address", match.group(0))
    lowered = text.lower()
    rest = sorted((i for i in found if i.kind != "name"), key=lambda i: lowered.find(i.value.lower()))
    return [i for i in found if i.kind == "name"] + rest


def tokens_for(items: list[Item]) -> dict[Item, str]:
    """Placeholder per item: [EMAIL], then [EMAIL_2], [EMAIL_3] for further items of a kind."""
    counts: dict[str, int] = {}
    result: dict[Item, str] = {}
    for item in items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
        n = counts[item.kind]
        result[item] = f"[{_TOKEN[item.kind]}]" if n == 1 else f"[{_TOKEN[item.kind]}_{n}]"
    return result


def _pattern(value: str) -> re.Pattern[str]:
    body = r"\s+".join(re.escape(part) for part in value.split())
    return re.compile(rf"(?<![\w]){body}(?![\w])" if value[:1].isalnum() and value[-1:].isalnum() else body, re.IGNORECASE)


def name_variants(name: str) -> list[str]:
    """Other ways the same name is written: without middle names or initials, and "Last, First"."""
    words = name.replace(",", " ").split()
    if len(words) < 3:
        return []
    first, last = words[0], words[-1]
    return [f"{first} {last}", f"{last}, {first}"]


def sanitize(text: str, items: list[Item]) -> str:
    """Replace every occurrence of every item with its placeholder (longest first). Names also match
    their shorter forms, so "Jane Q. Doe" in the header hides "Jane Doe" in a footer."""
    tokens = tokens_for(items)
    pairs = [(i.value, tokens[i]) for i in items]
    pairs += [(variant, tokens[i]) for i in items if i.kind == "name" for variant in name_variants(i.value)]
    for value, token in sorted(pairs, key=lambda pair: -len(pair[0])):
        text = _pattern(value).sub(lambda _m, t=token: t, text)
    return text


def used_tokens(sanitized: str) -> list[str]:
    """Placeholders present in a sanitized text, in order of first use."""
    return list(dict.fromkeys(m.group(0) for m in _TOKEN_RE.finditer(sanitized)))


def restore(text: str, items: list[Item]) -> str:
    """Put real values back in place of placeholders (unknown placeholders are left alone)."""
    by_token = {token: item.value for item, token in tokens_for(items).items()}
    return _TOKEN_RE.sub(lambda m: by_token.get(m.group(0), m.group(0)), text)


def leftovers(sanitized: str, kept: set[str]) -> list[str]:
    """Email, phone, or web-address patterns still in the text (ignoring values the owner chose to keep)."""
    keep = {k.lower() for k in kept}
    found: list[str] = []
    for pattern in [_EMAIL_RE, *_PHONE_RES, *_URL_RES]:
        for match in pattern.finditer(sanitized):
            value = _trim(match.group(0)).strip()
            if value.lower() not in keep and value not in found:
                found.append(value)
    return found
