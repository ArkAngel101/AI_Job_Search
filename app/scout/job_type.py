"""Permanent vs temporary: internships, co-ops and similar short placements are "temporary"."""
import re

# Title words only: descriptions mention "internship" incidentally (e.g. "our internship program").
_TEMPORARY_RE = re.compile(
    r"\b(interns?|internships?|co[\s\-]?ops?|apprentices?|apprenticeships?|trainees?|traineeships?|"
    r"temporary|temp|seasonal|summer analyst)\b",
    re.IGNORECASE,
)


def job_type(title: str | None) -> str:
    """"temporary" when the title names an internship, co-op or similar, else "permanent"."""
    return "temporary" if _TEMPORARY_RE.search(title or "") else "permanent"
