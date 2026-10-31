"""Rule-based extraction of structured fields from a job description. No LLM."""
import re
from functools import lru_cache
from typing import Any

from app.scout.scoring import detect_level
from app.skills import vocabulary

MAX_ITEMS = 30
MAX_ITEM_CHARS = 300

# --- sections ---------------------------------------------------------------------

_PREFERRED = (
    r"(preferred|desired|additional|bonus)( qualifications| skills| experience| requirements)?|nice to haves?|"
    r"good to have|bonus points|pluses|a plus|extra credit|it's a plus if( you)?"
)
_RESPONSIBILITIES = (
    r"(key |main |core |primary )?(responsibilities|duties)|what you'll do|what you will do|what you'll be doing|"
    r"your role|in this role|day to day|what you'll work on|what you'll own|the work"
)
_REQUIRED = (
    r"(minimum |basic |required |key |core )?(requirements|qualifications|skills|experience)( required)?|"
    r"what you'll need|what we're looking for|who you are|must haves?|you have|you bring|you should have|"
    r"about you|minimum"
)
_OTHER = (
    r"benefits|perks|about us|about the team|about the company|about the role|compensation|salary|pay|"
    r"equal opportunity|how to apply|what we offer|why join us|why .*|our values|location|working at .*|"
    r"life at .*|our mission|company|overview|the team"
)
_SECTION_PATTERNS = [("preferred", _PREFERRED), ("responsibilities", _RESPONSIBILITIES), ("required", _REQUIRED),
                     ("other", _OTHER)]


def _heading_class(line: str) -> str | None:
    """Which section a heading line starts, or None if the line is not a heading."""
    if line.startswith(("- ", "• ", "* ")) or line.endswith((".", ";", ",")):
        return None
    heading = re.sub(r"[^a-z' ]", " ", line.lower().replace("’", "'")).strip()
    heading = re.sub(r"\s+", " ", heading)
    if not heading or len(heading.split()) > 8:
        return None
    return next((name for name, pattern in _SECTION_PATTERNS if re.fullmatch(pattern, heading)), None)


def split_sections(text: str) -> dict[str, list[str]]:
    """Group lines under Responsibilities / Requirements / Preferred headings."""
    sections: dict[str, list[str]] = {"responsibilities": [], "required": [], "preferred": []}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        heading = _heading_class(line)
        if heading is not None:
            current = heading
        elif current in sections:
            item = re.sub(r"^[-•*]\s*", "", line).strip()
            if item and item not in sections[current] and len(sections[current]) < MAX_ITEMS:
                sections[current].append(item[:MAX_ITEM_CHARS])
    return sections


# --- skills -----------------------------------------------------------------------

_CASE_SENSITIVE_MAX_LEN = 5


@lru_cache(maxsize=4096)
def _skill_regex(skill: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in re.split(r"[\s\-]+", skill.strip()) if w]
    body = r"[\s\-]*".join(words) if len(words) > 1 else words[0]
    # Short all-caps skills (R, SQL, AWS) and "Excel" are matched case-sensitively to avoid ordinary words.
    sensitive = (len(skill) <= _CASE_SENSITIVE_MAX_LEN and skill.isupper()) or skill == "Excel"
    flags = 0 if sensitive else re.IGNORECASE
    plural = "s?" if len(skill) > 2 and skill[-1].isalpha() else ""
    edge = r"[\w+#&]" if len(skill) == 1 else r"[\w+#]"  # a lone "R" must not match inside "R&D"
    return re.compile(rf"(?<!{edge}){body}{plural}(?!{edge})", flags)


def find_skills(text: str, skills: list[str]) -> list[str]:
    """Skills from the list that appear in the text (whole words, vocabulary order, no duplicates)."""
    found: list[str] = []
    seen: set[str] = set()
    for skill in skills:
        key = skill.lower()
        if key not in seen and _skill_regex(skill).search(text):
            seen.add(key)
            found.append(skill)
    return found


# --- flags ------------------------------------------------------------------------

_FLAG_PATTERNS: dict[str, str] = {
    "work_authorization": (
        r"(authori[sz]ed|authori[sz]ation|eligib\w+|legally|right) to work (in|within) the (u\.?s\.?a?\.?|united states)|"
        r"work authori[sz]ation|legal right to work"
    ),
    "no_sponsorship": (
        r"(will not|won't|unable to|cannot|can't|do not|does not|not able to|not willing to|not)\s+"
        r"(be able to\s+)?(provide|offer|sponsor|support)\w*\s+[^.]{0,40}(sponsorship|visa)|"
        r"no (visa )?sponsorship|without (the need for )?(visa |employer )?sponsorship|"
        r"sponsorship (is |will be )?(not available|unavailable)|not require sponsorship|do(es)? not require (visa )?sponsorship|"
        r"unable to sponsor"
    ),
    "citizenship_required": (
        r"(u\.?s\.?|united states|american) citizen(s|ship)?|citizenship (is )?required|citizens only|"
        r"permanent resident"
    ),
    "clearance": r"(security |secret |government )?clearance|top secret|ts/sci|public trust",
}
_FLAG_RES = {name: re.compile(pattern, re.IGNORECASE) for name, pattern in _FLAG_PATTERNS.items()}


def find_flags(text: str) -> dict[str, Any]:
    """US work-authorization, sponsorship, citizenship, and clearance language, with short quotes."""
    flags: dict[str, Any] = {}
    matches: list[str] = []
    for name, pattern in _FLAG_RES.items():
        match = pattern.search(text)
        flags[name] = bool(match)
        if match:
            start, end = max(0, match.start() - 60), min(len(text), match.end() + 60)
            matches.append(" ".join(text[start:end].split()))
    flags["needs_sponsorship_or_citizenship"] = bool(
        flags["no_sponsorship"] or flags["citizenship_required"] or flags["clearance"]
    )
    flags["matches"] = matches
    return flags


# --- salary, remote, seniority --------------------------------------------------------

_MONEY = r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?\s?[kK])"
_SALARY_RE = re.compile(rf"{_MONEY}\s*(?:-|–|—|to)\s*{_MONEY}?")


def _money(value: str) -> float:
    value = value.replace(",", "").replace(" ", "")
    return float(value[:-1]) * 1000 if value[-1] in "kK" else float(value)


def find_salary(text: str) -> dict[str, Any] | None:
    """A "$120,000 - $150,000" or "$120k-$150k" range from the text, assuming yearly pay."""
    match = _SALARY_RE.search(text)
    if not match:
        return None
    low = _money(match.group(1))
    high = _money(match.group(2)) if match.group(2) else low
    if low < 1000 or high < low:
        return None
    return {"min": low, "max": high, "currency": "USD", "period": "year", "source": "text"}


_REMOTE_RE = re.compile(r"\b(fully remote|100% remote|remote[- ]first|work from home|remote position|remote role|"
                        r"this is a remote|remote[- ]friendly)\b", re.IGNORECASE)
_YEARS_RE = re.compile(
    r"\b(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*\+?\s*)?years?(?:'| of)?"
    r"(?: of)?(?: relevant| professional| industry| hands-on| practical| work)*\s*(?:experience|exp)?",
    re.IGNORECASE,
)
_ENTRY_RE = re.compile(r"\b(new grad(uate)?s?|recent graduates?|entry[- ]level|no experience (required|necessary)|"
                       r"early career|internship|co-?op|junior)\b", re.IGNORECASE)
_DEGREE_RE = re.compile(r"\b(ph\.?d\.?|doctorate|master'?s|m\.?s\.? (in|degree)|bachelor'?s|b\.?s\.? (in|degree))", re.IGNORECASE)


def seniority_signals(title: str, text: str) -> list[str]:
    """Level and experience hints: title level, years of experience asked, entry-level and degree wording."""
    signals: list[str] = []
    level = detect_level(title)
    if level:
        signals.append(f"title level: {level}")
    for match in _YEARS_RE.finditer(text):
        phrase = " ".join(match.group(0).split())
        if "experience" in phrase.lower() or match.group(2) or int(match.group(1)) >= 2:
            signals.append(f"years: {phrase}")
    signals += [f"entry-level wording: {' '.join(m.group(0).lower().split())}" for m in _ENTRY_RE.finditer(text)]
    signals += [f"degree: {' '.join(m.group(0).split())}" for m in _DEGREE_RE.finditer(text)]
    return list(dict.fromkeys(signals))[:12]


# --- main -------------------------------------------------------------------------

def extract(
    text: str,
    *,
    title: str,
    company: str,
    location: str,
    profile_skills: list[str],
    hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Structure a job description. `hints` may carry salary, remote, and location from the listing or page."""
    hints = hints or {}
    sections = split_sections(text)
    sectioned = any(sections.values())
    vocab = list(dict.fromkeys([*vocabulary(), *profile_skills]))

    required_text = "\n".join([*sections["required"], *sections["responsibilities"]]) if sectioned else text
    preferred_text = "\n".join(sections["preferred"])
    required = find_skills(required_text, vocab)
    preferred = [s for s in find_skills(preferred_text, vocab) if s not in required]
    if sectioned:  # skills mentioned only outside the named sections still count as asked for
        outside = [s for s in find_skills(text, vocab) if s not in required and s not in preferred]
        required += outside

    place = hints.get("location") or location
    hybrid = bool(re.search(r"\bhybrid\b", place or "", re.IGNORECASE))
    remote = bool(
        hints.get("remote") or "remote" in (place or "").lower() or (_REMOTE_RE.search(text) and not hybrid)
    )
    return {
        "title": title,
        "company": company,
        "location": place or "",
        "remote": remote,
        "salary": hints.get("salary") or find_salary(text),
        "responsibilities": sections["responsibilities"],
        "required_skills": required,
        "preferred_skills": preferred,
        "seniority_signals": seniority_signals(title, text),
        "flags": {**find_flags(text), "hybrid": hybrid},
        "sections_found": sectioned,
    }
