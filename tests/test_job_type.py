"""Permanent vs temporary classification from the job title."""
import pytest

from app.scout.job_type import job_type


@pytest.mark.parametrize("title", [
    "Machine Learning Intern", "Data Science Internship - Summer 2027", "AI Co-op", "ML Engineer Coop (Fall)",
    "Data Analyst Co Op", "Software Engineering Interns", "ML Apprentice", "Data Trainee", "Temporary Data Analyst",
])
def test_temporary_titles(title: str) -> None:
    assert job_type(title) == "temporary"


@pytest.mark.parametrize("title", [
    "Machine Learning Engineer", "Senior Data Scientist", "Internal Tools Engineer", "International Data Analyst",
    "Cooperative Systems Engineer", "Applied Scientist", "", None,
])
def test_permanent_titles(title: str | None) -> None:
    assert job_type(title) == "permanent"
