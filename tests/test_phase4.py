"""Phase 4: deterministic scoring."""
import json
from pathlib import Path

import pytest

from app.profile import Profile
from app.scout import scoring
from app.scout.location import classify_location, is_us_listing
from app.scout.models import RawListing
from app.scout.scoring import detect_level, domain_tokens, role_family, score_listing, skill_hits
from app.scout.top10 import load_listings, top
from app.skills import default_scoring_skills, vocabulary

FIXTURE = Path(__file__).parent / "fixtures" / "scoring_set.json"
PROFILE = Profile()  # entry-level, default titles, keywords and skills


def listing(title: str, location: str = "Boston, MA", description: str = "") -> RawListing:
    return RawListing(source="t", source_id="1", title=title, company="Acme", location=location,
                      description=description, url="https://t.example/1")


def groups() -> dict[str, list[tuple[RawListing, dict]]]:
    raw = json.loads(FIXTURE.read_text())
    items = load_listings(FIXTURE)
    result: dict[str, list[tuple[RawListing, dict]]] = {}
    for row, item in zip(raw, items, strict=True):
        result.setdefault(row["group"], []).append((item, score_listing(item, PROFILE, "first_cut")))
    return result


# --- the spec's "Done when" ---------------------------------------------------

def test_off_domain_roles_are_filtered() -> None:
    for item, result in groups()["offdomain"]:
        assert result["score"] < PROFILE.first_cut, (item.title, result["score"])


def test_entry_level_ai_ml_roles_rank_high() -> None:
    for item, result in groups()["entry"]:
        assert result["score"] >= PROFILE.final, (item.title, result["score"])
    best = top(load_listings(FIXTURE), PROFILE, "first_cut", n=5)
    assert {item.title for item, _ in best} <= {item.title for item, _ in groups()["entry"]}


def test_senior_roles_are_penalized_under_entry() -> None:
    by_title = {item.title: result for item, result in [*groups()["entry"], *groups()["senior"]]}
    intern = by_title["Machine Learning Engineer Intern"]
    senior = by_title["Senior Machine Learning Engineer"]
    assert senior["score"] < intern["score"]
    assert senior["breakdown"]["seniority"]["fraction"] <= 0.15
    for title in ("Staff Machine Learning Engineer", "Principal Applied Scientist", "Director of Data Science",
                  "Machine Learning Engineer (10+ years experience)"):
        result = by_title[title]
        assert result["flags"]["wrong_level"] and result["score"] < PROFILE.first_cut, title


def test_wrong_level_filter_is_off_for_seniority_any() -> None:
    staff = listing("Staff Machine Learning Engineer", description="Python PyTorch")
    entry = score_listing(staff, PROFILE, "first_cut")
    anyone = score_listing(staff, PROFILE.model_copy(update={"seniority": "any"}), "first_cut")
    assert entry["flags"]["wrong_level"] and not anyone["flags"]["wrong_level"]
    assert anyone["score"] > entry["score"] * 2
    assert anyone["breakdown"]["seniority"]["fraction"] == 1.0  # no level preference


def test_non_us_listings_are_rejected_before_scoring() -> None:
    kept = {item.title + item.location for item, _ in top(load_listings(FIXTURE), PROFILE, "first_cut", n=100)}
    assert "Machine Learning Engineer InternLondon, UK" not in kept
    assert "Data Scientist, New GradRemote - Europe" not in kept
    assert len(kept) == 24


# --- weights and result shape ---------------------------------------------------

def test_result_shape_and_weights() -> None:
    result = score_listing(listing("Machine Learning Engineer Intern", description="Python PyTorch"), PROFILE, "first_cut")
    assert set(result) == {"score", "breakdown", "flags"} and isinstance(result["score"], int)
    assert set(result["flags"]) == {"domain_anchor", "wrong_level", "needs_sponsorship_or_citizenship"}
    assert result["breakdown"]["title"]["points"] == 50.0 and result["breakdown"]["location"]["points"] == 12.0
    final = score_listing(listing("Machine Learning Engineer Intern", description="Python PyTorch"), PROFILE, "final")
    assert final["breakdown"]["title"]["points"] == 34.0 and final["breakdown"]["location"]["points"] == 10.0
    json.dumps(result)  # breakdown is stored as JSON
    with pytest.raises(ValueError):
        score_listing(listing("x"), PROFILE, "bogus")


def test_final_mode_reads_the_full_description() -> None:
    padding = "filler " * 400  # pushes the skills past the first-cut snippet
    item = listing("Machine Learning Engineer", description=padding + "Python PyTorch TensorFlow SQL")
    first = score_listing(item, PROFILE, "first_cut")["breakdown"]["skill"]["hits"]
    final = score_listing(item, PROFILE, "final")["breakdown"]["skill"]["hits"]
    assert first == ["machine learning"]  # only the title's skill is inside the snippet
    assert set(final) == {"machine learning", "Python", "PyTorch", "TensorFlow", "SQL"}


# --- title ----------------------------------------------------------------------

def test_domain_tokens_strip_generic_words_and_normalize_phrases() -> None:
    assert domain_tokens("Senior Machine Learning Engineer (m/f/d)") == {"ml"}
    assert domain_tokens("ML Engineer Intern - Remote, Full-time") == {"ml"}
    assert domain_tokens("Data Scientist, New Grad (2026)") == {"datascientist"}
    assert domain_tokens("Data Center Technician") == {"data", "center", "technician"}


def test_precision_factor_limits_one_lucky_keyword() -> None:
    clean = score_listing(listing("Machine Learning Engineer"), PROFILE, "first_cut")["breakdown"]["title"]["points"]
    lucky = score_listing(listing("Machine Learning Facilities Procurement Coordinator"), PROFILE, "first_cut")["breakdown"]["title"]["points"]
    assert clean == 50.0 and 0 < lucky < clean * 0.5


def test_role_family_penalty_for_sales_recruiter_assistant() -> None:
    for title in ("Sales Executive, Machine Learning", "Recruiter, AI", "Executive Assistant to the AI Lead"):
        assert role_family(title) == "off", title
    for title in ("Machine Learning Engineer", "Research Scientist", "Data Analyst, Marketing", "ML Developer"):
        assert role_family(title) == "technical", title
    related = [score_listing(listing(t), PROFILE, "first_cut")["breakdown"]["title"]["points"]
               for t in ("Machine Learning Engineer", "Machine Learning Scientist", "Machine Learning Researcher",
                         "Machine Learning Developer")]
    assert related == [50.0] * 4  # engineer, scientist, researcher, developer are related within AI/ML


def test_title_aliases_match() -> None:
    for title in ("ML Engineer", "Machine Learning Engineer", "Artificial Intelligence Engineer", "A.I. Engineer",
                  "Natural Language Processing Engineer", "Data Science Intern"):
        assert score_listing(listing(title), PROFILE, "first_cut")["breakdown"]["title"]["points"] == 50.0, title


# --- skills ---------------------------------------------------------------------

def test_skill_matching_rules() -> None:
    skills = ["Python", "machine learning", "scikit-learn", "natural language processing", "C++", "LLM"]
    hits = skill_hits(skills, "", "We use python and scikit learn. Experience with LLMs and C++ required.")
    assert hits == ["Python", "scikit-learn", "C++", "LLM"]  # plural LLMs ok; "machine learning" absent
    assert skill_hits(["natural language processing"], "", "text processing only") == []  # 1 of 3 words: no
    assert skill_hits(["natural language processing"], "", "language and natural processing") == ["natural language processing"]  # 3 of 3
    assert skill_hits(["natural language processing"], "", "natural text processing") == ["natural language processing"]  # 2 of 3 = 60%+
    assert skill_hits(["machine learning"], "Machine-Learning Engineer", "") == ["machine learning"]
    assert skill_hits(["R"], "", "grade report") == []  # whole words only


def test_skill_fraction_uses_the_profile_list() -> None:
    profile = PROFILE.model_copy(update={"scoring_skills": ["Python", "SQL", "Rust", "Go"]})
    result = score_listing(listing("Data Analyst", description="python and sql"), profile, "first_cut")
    assert result["breakdown"]["skill"]["fraction"] == 0.5
    assert score_listing(listing("Data Analyst"), profile.model_copy(update={"scoring_skills": []}), "first_cut")["breakdown"]["skill"]["fraction"] == 0


def test_default_skills_are_seeded_from_skills_yaml() -> None:
    assert PROFILE.scoring_skills == default_scoring_skills() and len(PROFILE.scoring_skills) == 16
    assert set(default_scoring_skills()) <= set(vocabulary())
    assert {"PyTorch", "RAG", "Docker"} <= set(vocabulary())


# --- seniority ------------------------------------------------------------------

@pytest.mark.parametrize(("title", "level"), [
    ("Machine Learning Intern", "entry"), ("Co-op Data Scientist", "entry"), ("New Grad ML Engineer", "entry"),
    ("Entry-Level Data Analyst", "entry"), ("Junior AI Engineer", "entry"), ("Associate Data Scientist", "entry"),
    ("Data Scientist II", "mid"), ("Senior ML Engineer", "senior"), ("Sr. Data Scientist", "senior"),
    ("Lead Machine Learning Engineer", "lead"), ("Staff ML Engineer", "principal"), ("Principal Scientist", "principal"),
    ("Director of Data Science", "manager"), ("VP of Engineering", "manager"), ("Machine Learning Engineer", None),
    ("Data Entry Specialist", None),
])
def test_detect_level(title: str, level: str | None) -> None:
    assert detect_level(title) == level


def test_seniority_credit_for_entry_profile() -> None:
    fractions = {t: score_listing(listing(t), PROFILE, "first_cut")["breakdown"]["seniority"]["fraction"]
                 for t in ("ML Intern", "ML Engineer", "ML Engineer II", "Senior ML Engineer", "Lead ML Engineer", "Principal ML Engineer")}
    assert fractions == {"ML Intern": 1.0, "ML Engineer": 0.6, "ML Engineer II": 0.5, "Senior ML Engineer": 0.15,
                         "Lead ML Engineer": 0.0, "Principal ML Engineer": 0.0}


def test_seniority_for_senior_profile() -> None:
    profile = PROFILE.model_copy(update={"seniority": "senior"})
    got = {t: score_listing(listing(t), profile, "first_cut")["breakdown"]["seniority"]["fraction"]
           for t in ("Senior ML Engineer", "ML Engineer II", "Lead ML Engineer", "ML Intern")}
    assert got == {"Senior ML Engineer": 1.0, "ML Engineer II": 0.5, "Lead ML Engineer": 0.5, "ML Intern": 0.15}


# --- location -------------------------------------------------------------------

@pytest.mark.parametrize(("location", "kind"), [
    ("San Francisco, CA", "us"), ("Boston, Massachusetts", "us"), ("New York, NY", "us"), ("Austin, TX or Remote", "remote_us"),
    ("Remote - US", "remote_us"), ("US Remote", "remote_us"), ("Remote (USA)", "remote_us"), ("United States", "us"),
    ("New Mexico, USA", "us"), ("Remote", "remote"), ("Worldwide", "unknown"), ("", "unknown"), ("Boston", "unknown"),
    ("London, UK", "non_us"), ("Remote - Europe", "non_us"), ("Bangalore, IN", "non_us"), ("Toronto, Canada", "non_us"),
    ("Remote, EMEA", "non_us"), ("Berlin", "non_us"), ("US or Canada", "us"),
])
def test_classify_location(location: str, kind: str) -> None:
    assert classify_location(location) == kind


def test_location_credit_levels() -> None:
    credit = {loc: score_listing(listing("ML Engineer", loc), PROFILE, "first_cut")["breakdown"]["location"]["fraction"]
              for loc in ("Boston, MA", "Remote - US", "Remote", "Boston", "London, UK")}
    assert credit == {"Boston, MA": 1.0, "Remote - US": 1.0, "Remote": 0.75, "Boston": 0.5, "London, UK": 0.0}
    assert is_us_listing("Boston") and not is_us_listing("London, UK")


# --- guards ---------------------------------------------------------------------

def test_relevance_gate_flags_missing_anchor() -> None:
    result = score_listing(listing("Mechanical Engineer Intern", description="CAD and thermal analysis"), PROFILE, "first_cut")
    assert result["flags"]["domain_anchor"] is False
    anchored = score_listing(listing("Mechanical Engineer Intern", description="Python scripting"), PROFILE, "first_cut")
    assert anchored["flags"]["domain_anchor"] is True  # a skill hit counts as an anchor


def test_relevance_gate_caps_at_38(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(scoring.WEIGHTS["first_cut"], "seniority", 90)
    result = score_listing(listing("Mechanical Engineer Intern"), PROFILE, "first_cut")
    assert result["flags"]["domain_anchor"] is False and result["score"] == 38
    assert result["breakdown"]["gate_capped"] is True
