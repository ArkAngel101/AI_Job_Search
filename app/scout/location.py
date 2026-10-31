"""US / remote-US / non-US classification of free-text job locations."""
import re

from app.scout.dedup import COUNTRY_TOKENS, REMOTE_WORDS, US_STATES, _clean

US_CODES = {code.upper() for code in US_STATES.values()}
_STATE_CODE_RE = re.compile(r"(?:,|^)\s*([A-Z]{2})\b")

NON_US_PLACES = (
    # countries
    "canada", "mexico", "brazil", "argentina", "colombia", "chile", "peru", "uk", "united kingdom", "england",
    "scotland", "wales", "ireland", "germany", "france", "spain", "portugal", "italy", "netherlands", "belgium",
    "switzerland", "austria", "sweden", "norway", "denmark", "finland", "poland", "czechia", "czech republic",
    "romania", "hungary", "greece", "turkey", "israel", "uae", "united arab emirates", "saudi arabia", "egypt",
    "nigeria", "kenya", "south africa", "india", "pakistan", "bangladesh", "sri lanka", "china", "japan", "korea",
    "singapore", "malaysia", "indonesia", "philippines", "vietnam", "thailand", "taiwan", "hong kong",
    "australia", "new zealand",
    # regions
    "europe", "emea", "apac", "latam", "asia", "africa", "middle east", "eu",
    # major cities
    "london", "paris", "berlin", "munich", "toronto", "vancouver", "montreal", "ottawa", "dublin", "amsterdam",
    "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "delhi", "gurgaon", "gurugram", "chennai", "tokyo",
    "sydney", "melbourne", "zurich", "stockholm", "madrid", "barcelona", "lisbon", "warsaw", "tel aviv",
    "sao paulo", "mexico city", "dubai", "shanghai", "beijing", "seoul", "taipei", "manila", "krakow",
    "bucharest", "kyiv", "tallinn",
)


def _word_regex(phrases: tuple[str, ...] | list[str]) -> re.Pattern[str]:
    return re.compile(r"\b(" + "|".join(sorted(phrases, key=len, reverse=True)) + r")\b")


_NON_US_RE = _word_regex(NON_US_PLACES)
_COUNTRY_RE = _word_regex(sorted(COUNTRY_TOKENS))
_STATE_NAME_RE = _word_regex(list(US_STATES))


def classify_location(location: str) -> str:
    """One of: "us", "remote_us", "remote" (unspecified), "unknown", "non_us"."""
    raw = (location or "").strip()
    if not raw:
        return "unknown"
    text = _clean(raw)
    remote = bool(REMOTE_WORDS.search(text))
    # Unambiguous US evidence: country words or full state names. Two-letter codes are weaker
    # ("Bangalore, IN" is not Indiana), so they only count when no non-US place is named.
    strong_us = bool(_COUNTRY_RE.search(text) or _STATE_NAME_RE.search(text))
    if _NON_US_RE.search(text) and not strong_us:
        return "non_us"
    weak_us = any(code in US_CODES for code in _STATE_CODE_RE.findall(raw))
    if strong_us or weak_us:
        return "remote_us" if remote else "us"
    return "remote" if remote else "unknown"


def is_us_listing(location: str) -> bool:
    """False only for locations that are clearly outside the US (unknown locations pass)."""
    return classify_location(location) != "non_us"
