"""Validate a tailored CV against the base CV in code (never by asking the model).

Rules:
  1. Every skill or tool named in the headline, summary, or bullets must appear in the base CV text.
  2. Not only known skills: any acronym, CamelCase term, letter-digit term, or capitalized word in the
     middle of a sentence that is absent from the base CV text is flagged, unless on a small allowlist.
  3. Every number or percentage must appear in the base CV.
  4. The same positions, the same number of bullets per position, and no new employers or roles.
"""
import re
from dataclasses import asdict, dataclass

from app.reader.extract import find_skills
from app.adapter.schema import BaseCV, TailoredCV
from app.skills import vocabulary

# Words that may legitimately appear capitalized or as acronyms without being in the CV.
ALLOWED = {
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "january",
    "february", "march", "april", "june", "july", "august", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "english", "us", "usa", "uk",
}
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#]*(?:[-.][A-Za-z0-9+#]+)*")
_NUMBER_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_PLACEHOLDER_RE = re.compile(r"\[[A-Z]+(?:_\d+)?\]")
_ACRONYM_RE = re.compile(r"^[A-Z]{2,6}s?$")
_CAMEL_RE = re.compile(r"[a-z][A-Z]|[A-Z][a-z]+[A-Z]")
_LETTER_DIGIT_RE = re.compile(r"[A-Za-z]\d|\d[A-Za-z]")


@dataclass
class Violation:
    """One broken rule: which rule, the offending text, and where it was found."""

    rule: str
    text: str
    where: str


@dataclass
class Report:
    """Result of validating a tailored CV."""

    passed: bool
    violations: list[Violation]

    def to_dict(self) -> dict:
        """JSON-friendly form stored in tailored_cvs.validation_json."""
        return {"passed": self.passed, "violations": [asdict(v) for v in self.violations]}


def role_key(*parts: str) -> tuple[str, ...]:
    return tuple(" ".join(p.lower().split()) for p in parts)


def _base_words(base_text: str) -> set[str]:
    words: set[str] = set()
    for token in _WORD_RE.findall(base_text):
        low = token.lower()
        words.add(low)
        words.update(part for part in re.split(r"[-.]", low) if part)
        if low.endswith("s"):
            words.add(low[:-1])
    return words


def _numbers(text: str) -> set[str]:
    return {m.group(0).replace(",", "").rstrip(".") for m in _NUMBER_RE.finditer(text)}


def _sentence_start(text: str, index: int) -> bool:
    before = text[:index].rstrip(" \t-•*·")
    return not before or before[-1] in ".!?:;\n"


def _suspicious_terms(text: str, base_words: set[str]) -> list[tuple[str, str]]:
    """(term, kind) for words that are not in the base CV and look like names, tools, or technologies."""
    found: list[tuple[str, str]] = []
    for match in _WORD_RE.finditer(text):
        word = match.group(0).rstrip(".")
        low = word.lower()
        parts = [p for p in re.split(r"[-.]", low) if p]
        if len(word) < 2 or low in ALLOWED or low in base_words or (parts and all(p in base_words for p in parts)):
            continue
        if low.endswith("s") and low[:-1] in base_words:
            continue
        if _ACRONYM_RE.match(word):
            found.append((word, "acronym"))
        elif _CAMEL_RE.search(word):
            found.append((word, "CamelCase term"))
        elif _LETTER_DIGIT_RE.search(word):
            found.append((word, "technical term"))
        elif word[0].isupper() and not _sentence_start(text, match.start()):
            found.append((word, "capitalized term"))
    return found


def validate(base: BaseCV, base_text: str, tailored: TailoredCV, extra_skills: list[str] | None = None) -> Report:
    """Check a tailored CV against the base CV and its text. `extra_skills` extends the skill vocabulary."""
    violations: list[Violation] = []
    base_words = _base_words(base_text)
    base_numbers = _numbers(base_text)
    vocab = list(dict.fromkeys([*vocabulary(), *(i for g in base.skills for i in g.items), *(extra_skills or [])]))

    texts = [("headline", tailored.headline), ("summary", tailored.summary)]
    for role in tailored.experience:
        texts += [(f"{role.company} / {role.role}", bullet) for bullet in role.bullets]

    for where, text in texts:
        for skill in find_skills(text, vocab):
            if not find_skills(base_text, [skill]):
                violations.append(Violation("skill not in CV", skill, where))
        for term, kind in _suspicious_terms(text, base_words):
            if not any(v.text.lower() == term.lower() and v.where == where for v in violations):
                violations.append(Violation(f"{kind} not in CV", term, where))
        for number in sorted(_numbers(text) - base_numbers):
            violations.append(Violation("number not in CV", number, where))
        for placeholder in _PLACEHOLDER_RE.findall(text):
            violations.append(Violation("unresolved placeholder", placeholder, where))

    violations += _structure_violations(base, tailored)
    return Report(passed=not violations, violations=violations)


def _structure_violations(base: BaseCV, tailored: TailoredCV) -> list[Violation]:
    out: list[Violation] = []
    base_roles = {role_key(e.company, e.role, e.start): e for e in base.experience}
    seen: set[tuple[str, ...]] = set()
    for role in tailored.experience:
        key = role_key(role.company, role.role, role.start)
        label = f"{role.company} / {role.role} / {role.start}"
        if key not in base_roles:
            out.append(Violation("employer or role not in CV", label, "experience"))
        elif key in seen:
            out.append(Violation("position listed twice", label, "experience"))
        elif len(role.bullets) != len(base_roles[key].bullets):
            out.append(Violation(
                f"bullet count changed ({len(base_roles[key].bullets)} in the CV, {len(role.bullets)} returned)",
                label, "experience"))
        seen.add(key)
    for key, entry in base_roles.items():
        if key not in seen:
            out.append(Violation("position missing from the answer", f"{entry.company} / {entry.role} / {entry.start}", "experience"))
    return out


def describe(report: Report) -> str:
    """The violations as a short list, used in the retry request and shown to the owner."""
    return "\n".join(f"- {v.rule}: \"{v.text}\" (in {v.where})" for v in report.violations)
