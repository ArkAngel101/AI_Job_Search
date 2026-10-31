"""The owner's search profile (defaults from the build spec; editable in Settings, Phase 10)."""
from typing import Literal

from pydantic import BaseModel, Field

from app.skills import default_scoring_skills

SOURCE_NAMES = (
    "greenhouse", "lever", "ashby", "workable",
    "adzuna", "remotive", "remoteok", "themuse", "jobicy",
)

Seniority = Literal["entry", "mid", "senior", "lead", "principal", "manager", "any"]


class Profile(BaseModel):
    """Targeting and scan settings used by Scout."""

    country: str = "US"
    titles: list[str] = Field(default_factory=lambda: [
        "AI Engineer", "Machine Learning Engineer", "ML Engineer", "Data Scientist",
        "Applied Scientist", "NLP Engineer", "Computer Vision Engineer",
        "ML Research Engineer", "Data Analyst", "MLOps Engineer",
    ])
    keywords: list[str] = Field(default_factory=lambda: [
        "machine learning", "data scientist", "AI engineer", "NLP", "computer vision",
        "LLM", "deep learning", "MLOps", "PyTorch", "applied scientist",
    ])
    seniority: Seniority = "entry"
    # Seeded from skills.yaml until a CV is uploaded; CV skills are merged in, never overwriting edits.
    scoring_skills: list[str] = Field(default_factory=default_scoring_skills)
    first_cut: int = 50
    final: int = 65
    pages: int = 2
    max_age_days: int = 30
    results_per_scan: int = 100
    enabled_sources: list[str] = Field(default_factory=lambda: list(SOURCE_NAMES))
