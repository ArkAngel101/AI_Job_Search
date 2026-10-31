"""Deterministic 0-100 job scoring against the profile. No LLM.

score = title + skill + seniority + location (weights depend on the mode), then two guards:
a relevance gate (no domain anchor caps the score) and a wrong-level filter (entry seekers).
"""
import math
import re
from typing import Any

from app.profile import Profile
from app.scout.dedup import normalize_title
from app.scout.location import classify_location
from app.scout.models import RawListing

WEIGHTS = {
    "first_cut": {"title": 50, "skill": 22, "seniority": 16, "location": 12},
    "final": {"skill": 42, "title": 34, "seniority": 14, "location": 10},
}
SNIPPET_CHARS = 1500  # first_cut looks at the title plus this much of the description
GATE_CAP = 38.0
WRONG_LEVEL_FACTOR = 0.35
FAMILY_FACTOR = 0.35

LEVELS = ("entry", "mid", "senior", "lead", "principal", "manager")

# --- title ------------------------------------------------------------------

# Phrases collapsed into single tokens on both sides, so "Machine Learning" and "ML" match.
_PHRASES = [
    (r"machine learning operations", "mlops"), (r"machine learning", "ml"), (r"ml ops", "mlops"),
    (r"artificial intelligence", "ai"), (r"natural language processing", "nlp"), (r"large language models?", "llm"),
    (r"computer vision", "computervision"), (r"deep learning", "deeplearning"),
    (r"data scientists?|data science", "datascientist"), (r"data analysts?|data analytics|data analysis", "dataanalyst"),
    (r"applied scientists?|applied science", "appliedscientist"),
]
_LEVEL_WORDS = {
    "senior", "sr", "junior", "jr", "intern", "interns", "internship", "co", "op", "coop", "lead", "staff", "principal",
    "associate", "entry", "level", "new", "grad", "graduate", "i", "ii", "iii", "iv", "director", "head", "vp",
    "manager", "chief", "mid", "early", "career", "trainee", "apprentice",
}
_ROLE_NOUNS = {
    "engineer", "engineers", "engineering", "developer", "developers", "scientist", "scientists", "science",
    "analyst", "analysts", "specialist", "researcher", "architect", "programmer", "consultant", "software",
    "analytics", "analysis",
}
_FILLER = {
    "remote", "full", "time", "fulltime", "part", "contract", "contractor", "temporary", "hybrid", "onsite", "on",
    "site", "us", "usa", "united", "states", "m", "f", "d", "w", "x", "all", "genders", "and", "of", "the", "for",
    "a", "an", "in", "at", "to", "with",
}
GENERIC_TOKENS = _LEVEL_WORDS | _ROLE_NOUNS | _FILLER

_TECH_NOUNS = re.compile(r"\b(engineer|developer|scientist|researcher|analyst|architect|programmer)s?\b")
_STRONG_OFF_FAMILY = re.compile(
    r"\b(sales|account executive|business development|bdr|sdr|account manager|recruit\w*|talent acquisition|sourcer)\b"
)
_WEAK_OFF_FAMILY = re.compile(
    r"\b(assistant|receptionist|secretary|clerk|administrator|admin|coordinator|customer success|customer support|"
    r"customer service|help ?desk|technician|nurse|physician|therapist|driver|warehouse|cashier|barista|chef|"
    r"mechanic|accountant|accounting|payroll|bookkeeper|attorney|paralegal|human resources|hr)\b"
)


def _normalize(title: str) -> str:
    text = normalize_title(title)
    for pattern, token in _PHRASES:
        text = re.sub(rf"\b(?:{pattern})\b", token, text)
    return text


def domain_tokens(title: str) -> set[str]:
    """Distinctive tokens of a title: generic level words, role nouns, filler, and numbers removed."""
    return {t for t in _normalize(title).split() if t not in GENERIC_TOKENS and not t.isdigit()}


def role_family(title: str) -> str:
    """"off" for sales/recruiting/assistant-type roles, otherwise "technical"."""
    text = normalize_title(title)
    if _STRONG_OFF_FAMILY.search(text):
        return "off"
    if _WEAK_OFF_FAMILY.search(text) and not _TECH_NOUNS.search(text):
        return "off"
    return "technical"


def _profile_targets_technical(profile: Profile) -> bool:
    return any(_TECH_NOUNS.search(normalize_title(t)) for t in profile.titles) or not profile.titles


def _title_component(listing: RawListing, profile: Profile) -> tuple[float, dict[str, Any]]:
    job = domain_tokens(listing.title)
    phrases = [t for t in (domain_tokens(p) for p in [*profile.titles, *profile.keywords]) if t]
    wanted = set().union(*phrases) if phrases else set()
    overlap = job & wanted
    family = role_family(listing.title)
    info: dict[str, Any] = {"overlap": sorted(overlap), "family": family}
    if not overlap:
        return 0.0, {**info, "fraction": 0.0}
    coverage = max(len(job & p) / len(p) for p in phrases)
    precision = len(overlap) / len(job)
    fraction = coverage * (0.5 + 0.5 * precision)  # one lucky keyword in a long off-topic title scores less
    if family == "off" and _profile_targets_technical(profile):
        fraction *= FAMILY_FACTOR
    return fraction, {**info, "coverage": round(coverage, 2), "precision": round(precision, 2), "fraction": round(fraction, 3)}


# --- skills -----------------------------------------------------------------

_STOP = {"and", "of", "for", "the", "with", "a", "an", "to", "in", "on"}


def _skill_text(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#\s]", " ", (text or "").lower()).split())


def _has_skill(skill: str, text: str, tokens: set[str]) -> bool:
    phrase = _skill_text(skill)
    if not phrase:
        return False
    if f" {phrase} " in f" {text} ":
        return True
    words = [w for w in phrase.split() if w not in _STOP]
    if len(words) < 2:
        return bool(words) and (words[0] in tokens or f"{words[0]}s" in tokens)
    found = sum(1 for w in words if w in tokens or f"{w}s" in tokens)
    return found >= math.ceil(0.6 * len(words))  # a multi-word skill counts at 60% of its words


def skill_hits(skills: list[str], title: str, body: str) -> list[str]:
    """Which of `skills` appear in the title plus body text."""
    text = _skill_text(f"{title}\n{body}")
    tokens = set(text.split())
    return [s for s in skills if _has_skill(s, text, tokens)]


# --- seniority --------------------------------------------------------------

_LEVEL_PATTERNS = [  # strongest level first
    ("manager", r"\b(director|head of|vp|vice president|chief|manager)\b"),
    ("principal", r"\b(principal|staff|distinguished|fellow)\b"),
    ("lead", r"\blead\b"),
    ("senior", r"\b(senior|sr|iii|iv)\b"),
    ("entry", r"\b(intern|interns|internship|co op|coop|new grad|new graduate|recent graduate|graduate|"
              r"entry level|junior|jr|associate|early career|apprentice|trainee)\b"),
    ("mid", r"\b(ii|mid|mid level|intermediate)\b"),
]
_WRONG_LEVEL_RE = re.compile(
    r"\b(staff|principal|distinguished|fellow|director|vp|vice president|head of|chief|(?:1\d|[2-9]\d) ?(?:years|yrs))\b"
)
_UNSPECIFIED_CREDIT = {"entry": 0.6}
_DISTANCE_CREDIT = {0: 1.0, 1: 0.5, 2: 0.15}


def detect_level(title: str) -> str | None:
    """Seniority level named in the title, or None when it names none."""
    text = normalize_title(title)
    return next((level for level, pattern in _LEVEL_PATTERNS if re.search(pattern, text)), None)


def _seniority_fraction(detected: str | None, target: str) -> float:
    if target == "any":
        return 1.0
    if detected is None:
        return _UNSPECIFIED_CREDIT.get(target, 0.7)
    return _DISTANCE_CREDIT.get(abs(LEVELS.index(detected) - LEVELS.index(target)), 0.0)


def is_wrong_level(title: str, profile: Profile) -> bool:
    """Entry seekers: staff, principal, director, VP, or 10+ years titles."""
    return profile.seniority == "entry" and bool(_WRONG_LEVEL_RE.search(normalize_title(title)))


# --- location ---------------------------------------------------------------

_LOCATION_CREDIT = {"us": 1.0, "remote_us": 1.0, "remote": 0.75, "unknown": 0.5, "non_us": 0.0}


# --- main -------------------------------------------------------------------

def score_listing(listing: RawListing, profile: Profile, mode: str) -> dict[str, Any]:
    """Score one listing. Returns {"score": int 0-100, "breakdown": dict, "flags": dict}."""
    if mode not in WEIGHTS:
        raise ValueError(f"Unknown scoring mode: {mode!r}")
    weights = WEIGHTS[mode]
    body = (listing.description or "")[:SNIPPET_CHARS] if mode == "first_cut" else (listing.description or "")

    title_fraction, title_info = _title_component(listing, profile)
    hits = skill_hits(profile.scoring_skills, listing.title, body)
    skill_fraction = len(hits) / len(profile.scoring_skills) if profile.scoring_skills else 0.0
    level = detect_level(listing.title)
    seniority_fraction = _seniority_fraction(level, profile.seniority)
    location_kind = classify_location(listing.location)
    location_fraction = _LOCATION_CREDIT[location_kind]

    fractions = {
        "title": title_fraction, "skill": skill_fraction,
        "seniority": seniority_fraction, "location": location_fraction,
    }
    total = sum(weights[name] * fractions[name] for name in weights)

    wrong_level = is_wrong_level(listing.title, profile)
    if wrong_level:
        total *= WRONG_LEVEL_FACTOR
    domain_anchor = bool(title_info["overlap"]) or bool(hits)
    capped = not domain_anchor and total > GATE_CAP
    if capped:
        total = GATE_CAP

    breakdown = {
        "mode": mode,
        "title": {"points": round(weights["title"] * title_fraction, 1), **title_info},
        "skill": {
            "points": round(weights["skill"] * skill_fraction, 1), "fraction": round(skill_fraction, 3),
            "hits": hits, "total": len(profile.scoring_skills),
        },
        "seniority": {
            "points": round(weights["seniority"] * seniority_fraction, 1), "fraction": seniority_fraction,
            "detected": level,
        },
        "location": {
            "points": round(weights["location"] * location_fraction, 1), "fraction": location_fraction,
            "kind": location_kind,
        },
        "wrong_level_factor": WRONG_LEVEL_FACTOR if wrong_level else 1.0,
        "gate_capped": capped,
    }
    flags = {"domain_anchor": domain_anchor, "wrong_level": wrong_level, "needs_sponsorship_or_citizenship": False}
    return {"score": max(0, min(100, round(total))), "breakdown": breakdown, "flags": flags}
