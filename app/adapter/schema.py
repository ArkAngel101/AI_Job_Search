"""Pydantic models for the structured CV."""
from pydantic import BaseModel, Field, model_validator


class Experience(BaseModel):
    """One job, internship, or research position."""

    company: str
    location: str
    start: str
    end: str
    role: str
    bullets: list[str]


class Education(BaseModel):
    """One school."""

    institution: str
    degree: str
    location: str
    start: str
    end: str
    details: list[str]


class SkillGroup(BaseModel):
    """A category of skills."""

    category: str
    items: list[str]


class Project(BaseModel):
    """One project."""

    name: str
    description: str
    bullets: list[str]


class Language(BaseModel):
    """A spoken language."""

    lang: str
    level: str


class SuggestedProfile(BaseModel):
    """Job-search suggestions derived from the CV."""

    titles: list[str]
    keywords: list[str]
    scoring_skills: list[str]
    seniority: str


class CVStructured(BaseModel):
    """What Claude returns for a sanitized CV (contact details are not part of it)."""

    name: str
    headline: str
    summary: str
    competencies: list[str]
    experience: list[Experience]
    education: list[Education]
    skills: list[SkillGroup]
    projects: list[Project]
    languages: list[Language]
    suggested_profile: SuggestedProfile


class Contact(BaseModel):
    """Contact details, re-attached by code (never sent to Claude)."""

    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    addresses: list[str] = Field(default_factory=list)
    linkedin: list[str] = Field(default_factory=list)
    github: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)


class BaseCV(BaseModel):
    """The validated base CV stored as base_cv.json."""

    name: str
    headline: str = ""
    summary: str = ""
    competencies: list[str] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[SkillGroup] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    contact: Contact = Field(default_factory=Contact)

    @model_validator(mode="after")
    def looks_like_a_cv(self) -> "BaseCV":
        if not self.name.strip():
            raise ValueError("the CV has no name")
        if not (self.experience or self.education or self.skills):
            raise ValueError("no experience, education, or skills were found")
        return self
