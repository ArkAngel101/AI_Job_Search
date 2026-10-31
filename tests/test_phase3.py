"""Phase 3: canonical URLs, normalization, de-duplication, and closed/reopened postings."""
import sqlite3
from pathlib import Path

import pytest

from app import config, db
from app.scout.dedup import (
    aliases, canonical_url, collapse, fuzzy_key, is_better, job_key,
    normalize_company, normalize_location, normalize_title,
)
from app.scout.models import RawListing


def listing(
    source: str = "adzuna", source_id: str = "1", title: str = "Machine Learning Engineer",
    company: str = "Acme", location: str = "Boston, MA", url: str = "", description: str = "short",
    **extra: object,
) -> RawListing:
    return RawListing(
        source=source, source_id=source_id, title=title, company=company, location=location,
        description=description, url=url or f"https://{source}.example/{source_id}", **extra,
    )


# --- canonical URLs ----------------------------------------------------------

def test_canonical_url_strips_tracking_and_normalizes() -> None:
    url = "HTTPS://Boards.Greenhouse.io/acme/jobs/123/?utm_source=x&utm_medium=y&gh_src=abc&ref=foo&source=li#app"
    assert canonical_url(url) == "https://boards.greenhouse.io/acme/jobs/123"


def test_canonical_url_strips_lever_source_and_keeps_real_params() -> None:
    a = canonical_url("https://jobs.lever.co/acme/1?lever-source[]=LinkedIn&team=ml")
    b = canonical_url("https://jobs.lever.co/acme/1/?team=ml&lever-source=Twitter")
    assert a == b == "https://jobs.lever.co/acme/1?team=ml"
    assert canonical_url("https://acme.com/careers?gh_jid=77&utm_campaign=z") == "https://acme.com/careers?gh_jid=77"


def test_canonical_url_fragments_and_empty() -> None:
    assert canonical_url("https://x.io/jobs/#/job/42") == "https://x.io/jobs#/job/42"  # route kept
    assert canonical_url("https://x.io/jobs/#.abc123") == "https://x.io/jobs"  # tracking dropped
    assert canonical_url("") == ""


# --- normalization -----------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Data Scientist (m/f/d)", "Data Scientist (w/m/x)", "Data Scientist - m/f/d", "Data Scientist (all genders)",
    "DATA  SCIENTIST!",
])
def test_title_strips_gender_tags(title: str) -> None:
    assert normalize_title(title) == "data scientist"


@pytest.mark.parametrize("name", ["Acme, Inc.", "Acme LLC", "ACME Technologies", "Acme Group Ltd", "acme GmbH", "Acme Corp", "Acme Co."])
def test_company_suffixes_stripped(name: str) -> None:
    assert normalize_company(name) == "acme"


def test_company_keeps_distinctive_words() -> None:
    assert normalize_company("Scale AI") == "scale ai"
    assert normalize_company("Group") == "group"  # never strip to empty


@pytest.mark.parametrize("loc", ["Remote", "Remote - US", "US Remote", "Anywhere", "San Francisco, CA (Remote)", "Work from home"])
def test_remote_is_its_own_location(loc: str) -> None:
    assert normalize_location(loc) == "remote"


def test_us_location_variants_compare_equal() -> None:
    assert normalize_location("Boston, Massachusetts") == normalize_location("Boston, MA, United States") == "boston ma"
    assert normalize_location("Washington, D.C.") == normalize_location("Washington DC")
    assert normalize_location("Zürich") == "zurich"


def test_fuzzy_key_requires_title_and_company() -> None:
    assert fuzzy_key("ML Engineer", "Acme Inc", "Boston, MA") == "ml engineer|acme|boston ma"
    assert fuzzy_key("", "Acme", "Boston") is None and fuzzy_key("ML", "", "Boston") is None


def test_job_key_is_stable_and_filename_safe() -> None:
    assert job_key("lever", "abc") == job_key("lever", "abc") != job_key("lever", "abd")
    assert len(job_key("lever", "abc")) == 16 and job_key("lever", "abc").isalnum()


# --- collapsing: the three match levels --------------------------------------

def test_match_level_1_same_source_and_id() -> None:
    a = listing(source="lever", source_id="9", title="ML Engineer", url="https://a.example/1")
    b = listing(source="lever", source_id="9", title="Totally Different", company="Other", location="Paris", url="https://b.example/2")
    assert len(collapse([a, b])) == 1


def test_match_level_2_same_canonical_url() -> None:
    a = listing(source="adzuna", source_id="1", title="ML Engineer", company="A", url="https://jobs.example/p/5?utm_source=x")
    b = listing(source="remotive", source_id="2", title="Other Title", company="B", location="Paris", url="https://JOBS.example/p/5/")
    merged = collapse([a, b])
    assert len(merged) == 1 and merged[0].sources == ["adzuna", "remotive"]


def test_match_level_3_same_title_company_location() -> None:
    a = listing(source="adzuna", source_id="1", title="Data Scientist (m/f/d)", company="Acme, Inc.", location="Boston, Massachusetts")
    b = listing(source="themuse", source_id="2", title="Data Scientist", company="ACME", location="Boston, MA")
    assert len(collapse([a, b])) == 1


def test_non_matching_listings_stay_separate() -> None:
    a = listing(source_id="1", title="Data Scientist")
    b = listing(source_id="2", title="ML Engineer")
    assert len(collapse([a, b])) == 2


def test_same_title_and_company_in_two_cities_stay_separate() -> None:
    boston = listing(source_id="1", location="Boston, MA")
    austin = listing(source_id="2", location="Austin, TX")
    remote = listing(source_id="3", location="Remote - US")
    assert len(collapse([boston, austin, remote])) == 3


def test_missing_company_never_merges_on_fuzzy() -> None:
    a = listing(source_id="1", company="")
    b = listing(source_id="2", company="")
    assert len(collapse([a, b])) == 2


# --- collapsing: cross-source duplicate --------------------------------------

def test_cross_source_duplicate_prefers_ats_and_records_all_sources() -> None:
    adz = listing(source="adzuna", source_id="a1", description="x" * 500, salary_min=100000.0, salary_max=130000.0,
                  created="2026-10-01", url="https://adzuna.example/a1")
    gh = listing(source="greenhouse", source_id="g1", description="short ATS text", board="greenhouse/acme",
                 url="https://boards.greenhouse.io/acme/jobs/1")
    muse = listing(source="themuse", source_id="m1", description="y" * 50)
    [merged] = collapse([adz, muse, gh])
    assert merged.listing.source == "greenhouse" and merged.listing.description == "short ATS text"
    assert merged.sources == ["greenhouse", "adzuna", "themuse"]
    assert merged.board == "greenhouse/acme"
    assert (merged.listing.salary_min, merged.listing.salary_max, merged.listing.created) == (100000.0, 130000.0, "2026-10-01")
    assert ("id", "adzuna:a1") in merged.aliases and ("id", "greenhouse:g1") in merged.aliases
    assert merged.key == job_key("greenhouse", "g1")


def test_without_ats_the_fullest_description_wins() -> None:
    [merged] = collapse([listing(source="adzuna", source_id="1", description="a"), listing(source="remoteok", source_id="2", description="a" * 80)])
    assert merged.listing.source == "remoteok"


def test_chained_matches_collapse_transitively() -> None:
    a = listing(source="adzuna", source_id="1", url="https://x/1", title="Alpha", company="C", location="Boston")
    b = listing(source="remotive", source_id="2", url="https://x/1", title="Beta", company="C", location="Boston")  # url match with a
    c = listing(source="themuse", source_id="3", url="https://y/3", title="Beta", company="C", location="Boston")  # fuzzy match with b
    assert len(collapse([a, b, c])) == 1


def test_is_better_prefers_ats_then_fuller_description() -> None:
    assert is_better("greenhouse", "tiny", "adzuna", "x" * 1000)
    assert not is_better("adzuna", "x" * 1000, "greenhouse", "tiny")
    assert is_better("adzuna", "longer text", "remoteok", "short")
    assert not is_better("adzuna", "same", "remoteok", "same")


# --- database: aliases, closed, reopened -------------------------------------

@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    db.init_db()
    return config.DATA_DIR


def add_job(key: str, board: str | None = None, closed: int = 0) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO jobs (key, title, board, closed) VALUES (?, 't', ?, ?)", (key, board, closed))


def closed_flags() -> dict[str, int]:
    with db.connect() as conn:
        return {r["key"]: r["closed"] for r in conn.execute("SELECT key, closed FROM jobs")}


def test_find_job_key_checks_strongest_level_first(data_dir: Path) -> None:
    with db.connect() as conn:
        db.add_aliases(conn, "by-fuzzy", [("fuzzy", "ml|acme|boston ma")])
        db.add_aliases(conn, "by-url", [("url", "https://x/1")])
        db.add_aliases(conn, "by-id", [("id", "lever:1")])
        probe = [("fuzzy", "ml|acme|boston ma"), ("url", "https://x/1"), ("id", "lever:1")]
        assert db.find_job_key(conn, probe) == "by-id"
        assert db.find_job_key(conn, probe[:2]) == "by-url"
        assert db.find_job_key(conn, probe[:1]) == "by-fuzzy"
        assert db.find_job_key(conn, [("id", "lever:nope")]) is None


def test_later_sighting_from_another_source_matches_stored_job(data_dir: Path) -> None:
    first = listing(source="adzuna", source_id="a1")
    later = listing(source="remoteok", source_id="r9", url="https://remoteok.example/r9")  # same title/company/city
    [merged] = collapse([first])
    with db.connect() as conn:
        db.add_aliases(conn, merged.key, merged.aliases)
        assert db.find_job_key(conn, aliases(later)) == merged.key
        db.add_aliases(conn, merged.key, aliases(later))
        assert db.sources_seen(conn, merged.key) == ["adzuna", "remoteok"]


def test_alias_owned_by_another_job_is_not_stolen(data_dir: Path) -> None:
    with db.connect() as conn:
        db.add_aliases(conn, "first", [("url", "https://x/1")])
        db.add_aliases(conn, "second", [("url", "https://x/1")])
        assert db.find_job_key(conn, [("url", "https://x/1")]) == "first"


def test_missing_job_on_complete_board_is_closed(data_dir: Path) -> None:
    for key in ("a", "b", "c"):
        add_job(key, board="greenhouse/acme")
    with db.connect() as conn:
        assert db.sync_closed(conn, {"greenhouse/acme"}, {"a", "b"}) == (1, 0)
    assert closed_flags() == {"a": 0, "b": 0, "c": 1}


def test_partial_or_failed_fetch_never_closes_jobs(data_dir: Path) -> None:
    add_job("a", board="greenhouse/acme")
    add_job("b", board="lever/beta")
    with db.connect() as conn:
        # lever/beta failed (not in completed_boards); greenhouse/acme completed and still lists "a".
        assert db.sync_closed(conn, {"greenhouse/acme"}, {"a"}) == (0, 0)
        assert db.sync_closed(conn, set(), set()) == (0, 0)
    assert closed_flags() == {"a": 0, "b": 0}


def test_reappearing_job_is_reopened(data_dir: Path) -> None:
    add_job("a", board="greenhouse/acme", closed=1)
    add_job("b", board="greenhouse/acme", closed=1)
    with db.connect() as conn:
        assert db.sync_closed(conn, {"greenhouse/acme"}, {"a"}) == (0, 1)
    assert closed_flags() == {"a": 0, "b": 1}


def test_jobs_without_a_board_or_on_other_boards_are_left_alone(data_dir: Path) -> None:
    add_job("agg", board=None)
    add_job("other", board="lever/beta")
    add_job("mine", board="greenhouse/acme")
    with db.connect() as conn:
        assert db.sync_closed(conn, {"greenhouse/acme"}, set()) == (1, 0)
    assert closed_flags() == {"agg": 0, "other": 0, "mine": 1}


def test_job_still_listed_by_another_source_stays_open(data_dir: Path) -> None:
    """The ATS board dropped it, but an aggregator still lists the same job (same key seen)."""
    add_job("dup", board="greenhouse/acme")
    with db.connect() as conn:
        assert db.sync_closed(conn, {"greenhouse/acme"}, {"dup"}) == (0, 0)
    assert closed_flags() == {"dup": 0}


# --- schema migration --------------------------------------------------------

def test_phase1_database_is_migrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    old = sqlite3.connect(tmp_path / "forge.db")
    old.execute("CREATE TABLE jobs (key TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL DEFAULT 'seen')")
    old.execute("INSERT INTO jobs (key, title) VALUES ('old', 'kept')")
    old.commit()
    old.close()
    db.init_db()
    db.init_db()  # idempotent
    with db.connect() as conn:
        assert "board" in {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert conn.execute("SELECT title FROM jobs WHERE key = 'old'").fetchone()["title"] == "kept"
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "job_aliases" in tables
