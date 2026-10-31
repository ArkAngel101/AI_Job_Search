"""Phase 6: Job Reader (no LLM), pasted URLs and text, and safe page fetching."""
import json
import sqlite3
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app
from app.reader import add_job as add_job_module
from app.reader import reader, safe_fetch
from app.reader.add_job import AddJobError, add_job
from app.reader.ats_urls import fetch_ats_job, parse_ats_url
from app.reader.extract import extract, find_flags, find_salary, find_skills, split_sections
from app.reader.page import looks_unreadable, page_text, parse_job_posting
from app.reader.safe_fetch import FetchedPage, PageFetchError, UnsafeURL, fetch_public_page, is_public_ip, validate_url
from app.scout.boards import Board, load_boards
from app.scout.dedup import MergedListing, aliases
from app.scout.http import ScoutClient
from app.scout.models import RawListing

FIXTURES = Path(__file__).parent / "fixtures"
HEADERS = {"X-Forge-Request": "1"}


def fx(name: str) -> Any:
    path = FIXTURES / name
    return json.loads(path.read_text()) if name.endswith(".json") else path.read_text()


# --- extraction ---------------------------------------------------------------------------

def sample() -> dict[str, Any]:
    return extract(fx("jd_sample.txt"), title="Machine Learning Engineer, New Grad", company="Acme",
                   location="Austin, TX", profile_skills=[])


def test_sections_are_split_by_heading() -> None:
    sections = split_sections(fx("jd_sample.txt"))
    assert sections["responsibilities"] == [
        "Train and evaluate deep learning models in PyTorch", "Build data pipelines with Spark and Airflow",
        "Deploy models to AWS with Docker"]
    assert "Proficiency in Python and SQL" in sections["required"] and len(sections["required"]) == 4
    assert sections["preferred"] == ["Experience with Kubernetes", "Knowledge of LLMs and RAG", "Publications in NLP"]


@pytest.mark.parametrize("heading", [
    "Responsibilities", "What you'll do", "What you’ll do:", "Key Responsibilities", "Requirements",
    "Minimum Qualifications", "Basic qualifications", "Qualifications:", "Nice to have", "Bonus", "Preferred qualifications",
])
def test_heading_variants_open_sections(heading: str) -> None:
    sections = split_sections(f"{heading}\n- Item one\n- Item two")
    assert sum(len(v) for v in sections.values()) == 2


def test_other_headings_close_a_section() -> None:
    sections = split_sections("Requirements\n- Python\nBenefits\n- Free lunch\n- Gym")
    assert sections["required"] == ["Python"]


def test_skills_split_into_required_and_preferred() -> None:
    jd = sample()
    assert set(jd["required_skills"]) >= {"PyTorch", "deep learning", "Spark", "Airflow", "AWS", "Docker", "Python",
                                         "SQL", "machine learning", "statistics"}
    assert set(jd["preferred_skills"]) == {"Kubernetes", "LLM", "RAG", "NLP"}
    assert not set(jd["required_skills"]) & set(jd["preferred_skills"])


def test_skills_mentioned_outside_named_sections_count_as_required() -> None:
    jd = extract("About us\nWe love TensorFlow.\nRequirements\n- Python", title="x", company="x", location="",
                 profile_skills=[])
    assert jd["required_skills"] == ["Python", "TensorFlow"]


def test_profile_skills_extend_the_vocabulary() -> None:
    jd = extract("Requirements\n- Experience with Rust and Python", title="x", company="x", location="",
                 profile_skills=["Rust"])
    assert "Rust" in jd["required_skills"]


@pytest.mark.parametrize(("text", "expected"), [
    ("R&D team", []), ("Experience with R and Python", ["Python", "R"]), ("We use Python/R daily", ["Python", "R"]),
    ("JavaScript developer", []), ("Java developer", ["Java"]), ("strong sql skills", []), ("Strong SQL skills", ["SQL"]),
    ("C++ and C#", ["C++"]), ("You will excel at teamwork", []), ("Proficient in Excel", ["Excel"]),
    ("scikit learn and scikit-learn", ["scikit-learn"]), ("building LLMs", ["LLM"]),
    ("machine-learning pipelines", ["machine learning"]), ("postgresql tuning", ["PostgreSQL"]),
])
def test_skill_matching_is_precise(text: str, expected: list[str]) -> None:
    vocab = ["Python", "R", "Java", "SQL", "C++", "Excel", "scikit-learn", "LLM", "machine learning", "PostgreSQL"]
    assert sorted(find_skills(text, vocab)) == sorted(expected)


def test_seniority_signals() -> None:
    signals = sample()["seniority_signals"]
    assert "title level: entry" in signals and "years: 0-2 years of experience" in signals
    assert any(s.startswith("degree: Bachelor") for s in signals)
    senior = extract("Requirements\n- 8+ years of experience in ML\n- PhD preferred", title="Staff ML Engineer",
                     company="x", location="", profile_skills=[])["seniority_signals"]
    assert "title level: principal" in senior and "years: 8+ years of experience" in senior
    assert any("PhD" in s for s in senior)


def test_flags_for_authorization_and_no_sponsorship() -> None:
    flags = sample()["flags"]
    assert flags["work_authorization"] and flags["no_sponsorship"] and flags["needs_sponsorship_or_citizenship"]
    assert not flags["citizenship_required"] and not flags["clearance"]
    assert any("visa sponsorship" in m for m in flags["matches"])


def test_flags_for_citizenship_and_clearance() -> None:
    flags = find_flags("Applicants must be a U.S. citizen and able to obtain a Secret clearance.")
    assert flags["citizenship_required"] and flags["clearance"] and flags["needs_sponsorship_or_citizenship"]
    assert find_flags("Active TS/SCI required.")["clearance"]


@pytest.mark.parametrize("text", [
    "We sponsor visas for qualified candidates.", "Visa sponsorship is available.", "We offer relocation help.",
    "Great team culture.",
])
def test_no_false_alarm_on_friendly_sponsorship_wording(text: str) -> None:
    assert find_flags(text)["needs_sponsorship_or_citizenship"] is False


@pytest.mark.parametrize("text", [
    "We are not able to offer visa sponsorship.", "No visa sponsorship is provided.", "We do not sponsor work visas.",
    "Candidates must not require sponsorship now or in the future.", "Sponsorship is not available for this role.",
    "Unable to sponsor employment visas.",
])
def test_no_sponsorship_variants_are_flagged(text: str) -> None:
    assert find_flags(text)["no_sponsorship"] is True, text


def test_salary_and_remote_extraction() -> None:
    assert find_salary("Pay: $120,000 - $150,000 a year") == {
        "min": 120000.0, "max": 150000.0, "currency": "USD", "period": "year", "source": "text"}
    assert find_salary("$95k–$120k")["min"] == 95000.0 and find_salary("no pay info") is None
    assert find_salary("$5 - $2") is None
    assert sample()["remote"] is True and sample()["salary"]["max"] == 140000.0
    hybrid = extract("Great job", title="x", company="x", location="Boston, MA (Hybrid)", profile_skills=[])
    assert hybrid["remote"] is False and hybrid["flags"]["hybrid"] is True
    assert extract("Great job", title="x", company="x", location="Remote - US", profile_skills=[])["remote"] is True
    hinted = extract("Great job", title="x", company="x", location="", profile_skills=[],
                     hints={"salary": {"min": 1.0, "max": 2.0}, "remote": True})
    assert hinted["remote"] and hinted["salary"] == {"min": 1.0, "max": 2.0}


def test_text_without_headings_still_yields_skills() -> None:
    jd = extract("We use Python and PyTorch daily.", title="x", company="x", location="", profile_skills=[])
    assert jd["sections_found"] is False and jd["required_skills"] == ["Python", "PyTorch"]
    assert jd["responsibilities"] == [] and jd["preferred_skills"] == []


# --- pages ---------------------------------------------------------------------------------

def test_json_ld_job_posting_parses() -> None:
    posting = parse_job_posting(fx("page_jsonld.html"))
    assert posting is not None
    assert (posting["title"], posting["company"], posting["location"]) == ("Machine Learning Engineer", "Acme Corp", "Austin, TX, US")
    assert posting["remote"] is True and posting["created"] == "2026-10-20"
    assert posting["salary"] == {"min": 120000.0, "max": 150000.0, "currency": "USD", "period": "year", "source": "json-ld"}
    assert "Train models in PyTorch" in posting["description"] and "<" not in posting["description"]


def test_json_ld_graph_and_bad_json() -> None:
    html = ('<script type="application/ld+json">{bad json</script>'
            '<script type="application/ld+json">{"@graph": [{"@type": "WebPage"}, {"@type": ["JobPosting"], "title": "Data Scientist",'
            ' "description": "<p>Do data science</p>"}]}</script>')
    assert parse_job_posting(html)["title"] == "Data Scientist"
    assert parse_job_posting("<html><body>no data</body></html>") is None


def test_page_text_drops_navigation_scripts_and_footer() -> None:
    text = page_text(fx("page_plain.html"))
    assert "Data Scientist" in text and "A/B testing" in text
    assert "Log in" not in text and "track()" not in text and "Copyright" not in text


def test_unreadable_page_detection() -> None:
    assert looks_unreadable(page_text(fx("page_blocked.html")), 200)
    assert looks_unreadable("x" * 5000, 403) and looks_unreadable("x" * 5000, 429) and looks_unreadable("short", 200)
    assert looks_unreadable("Please verify you are human. " + "filler " * 100, 200)
    assert not looks_unreadable(page_text(fx("page_plain.html")), 200)
    assert not looks_unreadable("A real job description. " * 100, 200)


# --- ATS URLs -------------------------------------------------------------------------------

@pytest.mark.parametrize(("url", "expected"), [
    ("https://boards.greenhouse.io/acme/jobs/4001", ("greenhouse", "acme", "4001")),
    ("https://job-boards.greenhouse.io/acme/jobs/4001/", ("greenhouse", "acme", "4001")),
    ("https://jobs.lever.co/acme/aaaaaaaa-1111-2222-3333-444444444444", ("lever", "acme", "aaaaaaaa-1111-2222-3333-444444444444")),
    ("https://jobs.ashbyhq.com/acme/bbbbbbbb-1111-2222-3333-444444444444", ("ashby", "acme", "bbbbbbbb-1111-2222-3333-444444444444")),
    ("https://apply.workable.com/acme/j/AAA111/", ("workable", "acme", "AAA111")),
])
def test_parse_ats_url(url: str, expected: tuple[str, str, str]) -> None:
    ref = parse_ats_url(url)
    assert ref is not None and (ref.ats, ref.slug, ref.job_id) == expected


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/jobs/view/123", "https://boards.greenhouse.io/acme", "https://jobs.lever.co/acme",
    "https://boards.greenhouse.io/../jobs/4001", "ftp://boards.greenhouse.io/acme/jobs/1", "https://evil.example/acme/jobs/1",
    "not a url",
])
def test_parse_ats_url_rejects_others(url: str) -> None:
    assert parse_ats_url(url) is None


# --- fixtures for the DB-backed tests ---------------------------------------------------------

class Web:
    """Mock internet for ATS API calls, keyed by host."""

    def __init__(self) -> None:
        self.data: dict[str, tuple[int, Any]] = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.data.get(request.url.host, (404, {}))
        return httpx.Response(status, json=body)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Web]:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    db.init_db()
    (config.DATA_DIR / "boards.yaml").write_text("greenhouse:\n  - {slug: acme, name: Acme Inc}\n")
    web = Web()
    web.data = {"boards-api.greenhouse.io": (200, fx("greenhouse.json")), "api.lever.co": (200, fx("lever.json")),
                "api.ashbyhq.com": (200, fx("ashby.json")), "apply.workable.com": (200, fx("workable.json"))}
    monkeypatch.setattr(
        add_job_module, "make_client", lambda: ScoutClient(transport=httpx.MockTransport(web.handler), sleep=lambda s: None)
    )

    def no_fetch(url: str) -> FetchedPage:
        raise AssertionError(f"unexpected page fetch: {url}")

    monkeypatch.setattr(reader, "fetch_page", no_fetch)
    yield web


def store(source: str, description: str, url: str = "https://example.test/job", title: str = "Machine Learning Engineer",
          location: str = "Boston, MA", source_id: str = "1") -> str:
    item = RawListing(source=source, source_id=source_id, title=title, company="Acme", location=location,
                      description=description, url=url)
    merged = MergedListing(listing=item, sources=[source], aliases=aliases(item))
    with db.connect() as conn:
        key, _ = db.upsert_seen(conn, merged, 70, {"mode": "first_cut"}, run_id=1)
    return key


def row(key: str) -> sqlite3.Row:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone()


def page(html: str, status: int = 200, url: str = "https://example.test/job") -> Callable[[str], FetchedPage]:
    return lambda _url: FetchedPage(url=url, status=status, content_type="text/html", text=html)


# --- reading a stored job -----------------------------------------------------------------------

def test_ats_job_is_read_from_its_own_description_without_a_fetch(env: Web) -> None:
    key = store("greenhouse", fx("jd_sample.txt"), title="Machine Learning Engineer, New Grad")
    jd = reader.read_job(key)
    stored = row(key)
    assert jd["read_via"] == "ats" and jd["partial"] is False and stored["status"] == "read"
    assert json.loads(stored["jd_json"])["required_skills"] == jd["required_skills"]
    breakdown = json.loads(stored["score_breakdown_json"])
    assert breakdown["mode"] == "final" and breakdown["flags"]["needs_sponsorship_or_citizenship"] is True
    assert stored["score"] != 70  # re-scored at final mode (the stored first-cut score was 70)
    with db.connect() as conn:
        log = conn.execute("SELECT agent_name, model_used, status FROM activity_log").fetchall()
    assert [tuple(r) for r in log] == [("job-reader", "none", "completed")]


def test_a_job_already_read_is_cached_and_never_refetched(env: Web, monkeypatch: pytest.MonkeyPatch) -> None:
    key = store("adzuna", "short snippet")
    monkeypatch.setattr(reader, "fetch_page", page(fx("page_jsonld.html")))
    first = reader.read_job(key)
    monkeypatch.setattr(reader, "fetch_page", lambda url: (_ for _ in ()).throw(AssertionError("refetched")))
    assert reader.read_job(key) == first
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0] == 1


def test_thin_listing_is_enriched_from_json_ld(env: Web, monkeypatch: pytest.MonkeyPatch) -> None:
    key = store("adzuna", "Train models in…")
    monkeypatch.setattr(reader, "fetch_page", page(fx("page_jsonld.html")))
    jd = reader.read_job(key)
    assert jd["read_via"] == "json-ld" and jd["partial"] is False and jd["note"] is None
    assert "PyTorch" in jd["required_skills"] and jd["salary"]["min"] == 120000.0 and jd["remote"] is True


def test_thin_listing_falls_back_to_page_text(env: Web, monkeypatch: pytest.MonkeyPatch) -> None:
    key = store("adzuna", "Forecasting…")
    monkeypatch.setattr(reader, "fetch_page", page(fx("page_plain.html")))
    jd = reader.read_job(key)
    assert jd["read_via"] == "page-text" and "A/B testing" in jd["required_skills"] and "pandas" in jd["required_skills"]


@pytest.mark.parametrize("fetcher", [
    page(fx("page_blocked.html")), page("<html>forbidden</html>", status=403),
    lambda url: (_ for _ in ()).throw(UnsafeURL("The URL points to a private or non-public address")),
    lambda url: (_ for _ in ()).throw(PageFetchError("request failed (ConnectError)")),
], ids=["login-wall", "403", "unsafe", "network"])
def test_blocked_page_falls_back_to_listing_and_is_marked_partial(
    env: Web, monkeypatch: pytest.MonkeyPatch, fetcher: Callable[[str], FetchedPage]
) -> None:
    key = store("adzuna", "We use Python and PyTorch.")
    monkeypatch.setattr(reader, "fetch_page", fetcher)
    jd = reader.read_job(key)
    assert jd["partial"] is True and jd["read_via"] == "listing" and jd["note"]
    assert jd["required_skills"] == ["Python", "PyTorch"] and row(key)["status"] == "read"


def test_full_listing_description_skips_the_page_fetch(env: Web) -> None:
    key = store("remoteok", "Requirements\n- Python\n" + "Details about the role. " * 60)
    jd = reader.read_job(key)  # the fixture raises if a page is fetched
    assert jd["read_via"] == "listing" and jd["partial"] is False and jd["required_skills"] == ["Python"]


def test_unknown_key_raises(env: Web) -> None:
    with pytest.raises(KeyError):
        reader.read_job("nope")


def test_no_llm_is_called_or_imported(env: Web) -> None:
    reader.read_job(store("greenhouse", fx("jd_sample.txt")))
    add_job(text="Machine Learning Engineer\n" + "We are hiring. " * 10)
    assert "anthropic" not in sys.modules and not [m for m in sys.modules if m.startswith("app.adapter.")]


# --- add by text ------------------------------------------------------------------------------------

TEXT = "Data Scientist, Growth\nBeta Labs\nRequirements\n- Python and SQL\n- Experience with A/B testing\nUS only."


def test_paste_text_creates_and_reads_a_job(env: Web) -> None:
    result = add_job(text=TEXT, company="Beta Labs", location="Remote - US")
    assert result["created"] is True and result["read_via"] == "pasted-text" and result["partial"] is False
    job = result["job"]
    assert (job["title"], job["company"], job["source"], job["status"], job["user_status"]) == (
        "Data Scientist, Growth", "Beta Labs", "pasted", "read", "new")
    assert job["new"] is False and job["score"] > 0
    assert row(result["key"])["jd_json"]


def test_pasting_the_same_text_twice_does_not_duplicate(env: Web) -> None:
    first = add_job(text=TEXT)
    second = add_job(text=TEXT)
    assert second["key"] == first["key"] and second["created"] is False
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0] == 1  # read once


def test_paste_text_validation(env: Web) -> None:
    with pytest.raises(AddJobError):
        add_job()
    with pytest.raises(AddJobError, match="too short"):
        add_job(text="hi")
    with pytest.raises(AddJobError) as exc:
        add_job(url="javascript:alert(1)")
    assert exc.value.status == 400
    result = add_job(text=TEXT, url="https://example.test/posting")  # text wins; the URL is kept as a link only
    assert result["job"]["url"] == "https://example.test/posting"


# --- add by ATS URL ------------------------------------------------------------------------------------

def test_add_greenhouse_url_uses_the_official_api(env: Web) -> None:
    result = add_job(url="https://boards.greenhouse.io/acme/jobs/4001")
    assert [r.url.host for r in env.requests] == ["boards-api.greenhouse.io"]
    job = result["job"]
    assert (job["title"], job["company"], job["source"], job["status"]) == (
        "Machine Learning Engineer, New Grad", "Acme Inc", "greenhouse", "read")
    assert result["read_via"] == "ats" and result["offer_add_board"] is None  # acme is already in boards.yaml
    assert row(result["key"])["board"] == "greenhouse/acme"


@pytest.mark.parametrize(("url", "ats", "title"), [
    ("https://jobs.lever.co/newco/lv-1", "lever", "Data Scientist, Intern"),
    ("https://jobs.ashbyhq.com/newco/ab-1", "ashby", "AI Engineer"),
    ("https://apply.workable.com/newco/j/AAA111", "workable", "Open-Source Machine Learning Engineer"),
    ("https://job-boards.greenhouse.io/newco/jobs/4001", "greenhouse", "Machine Learning Engineer, New Grad"),
])
def test_add_each_ats_and_offer_to_add_the_board(env: Web, url: str, ats: str, title: str) -> None:
    if ats in ("lever", "ashby"):  # fixture ids are not UUID-shaped; use shaped ids in the mocked board data
        data = fx(f"{ats}.json")
        postings = data if ats == "lever" else data["jobs"]
        new_id = "aaaaaaaa-1111-2222-3333-444444444444"
        postings[0]["id"] = new_id
        env.data["api.lever.co" if ats == "lever" else "api.ashbyhq.com"] = (200, data)
        url = f"https://jobs.{'lever.co' if ats == 'lever' else 'ashbyhq.com'}/newco/{new_id}"
    result = add_job(url=url)
    assert result["job"]["title"] == title and result["job"]["source"] == ats
    assert result["offer_add_board"] == {"ats": ats, "slug": "newco"}
    assert row(result["key"])["board"] is None  # not on the closed-marking list until the owner adds it
    assert result["job"]["company"] == "Newco" or ats == "workable"


def test_ats_url_errors_ask_for_pasted_text(env: Web) -> None:
    env.data["boards-api.greenhouse.io"] = (404, {})
    with pytest.raises(AddJobError) as missing_board:
        add_job(url="https://boards.greenhouse.io/ghost/jobs/1")
    assert missing_board.value.needs_text and "not found" in str(missing_board.value)

    env.data["boards-api.greenhouse.io"] = (200, fx("greenhouse.json"))
    with pytest.raises(AddJobError) as closed_job:
        add_job(url="https://boards.greenhouse.io/acme/jobs/999999")
    assert closed_job.value.needs_text and "paste" in str(closed_job.value).lower()

    env.data["boards-api.greenhouse.io"] = (500, {})
    with pytest.raises(AddJobError) as down:
        add_job(url="https://boards.greenhouse.io/acme/jobs/4001")
    assert down.value.needs_text and "HTTP 500" in str(down.value)


def test_fetch_ats_job_picks_the_matching_id(env: Web) -> None:
    ref = parse_ats_url("https://boards.greenhouse.io/acme/jobs/4002")
    with ScoutClient(transport=httpx.MockTransport(env.handler)) as client:
        assert fetch_ats_job(ref, client).title == "Account Executive"
        assert fetch_ats_job(parse_ats_url("https://boards.greenhouse.io/acme/jobs/1"), client) is None


# --- add by other URLs ---------------------------------------------------------------------------------

def test_add_url_reads_json_ld(env: Web, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "fetch_page", page(fx("page_jsonld.html"), url="https://careers.example/jobs/9?utm_source=x"))
    result = add_job(url="https://careers.example/jobs/9?utm_source=x")
    job = result["job"]
    assert result["read_via"] == "json-ld" and job["source"] == "web" and job["company"] == "Acme Corp"
    assert (job["title"], job["location"]) == ("Machine Learning Engineer", "Austin, TX, US")
    again = add_job(url="https://careers.example/jobs/9")  # same canonical URL: no duplicate
    assert again["key"] == result["key"] and again["created"] is False


def test_add_url_falls_back_to_page_text(env: Web, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "fetch_page", page(fx("page_plain.html"), url="https://www.betalabs.example/jobs/ds"))
    result = add_job(url="https://www.betalabs.example/jobs/ds")
    assert result["read_via"] == "page-text"
    assert (result["job"]["title"], result["job"]["company"]) == ("Data Scientist", "Beta Labs")


@pytest.mark.parametrize("fetcher", [
    page(fx("page_blocked.html")), page("<html>blocked</html>", status=403),
    lambda url: (_ for _ in ()).throw(PageFetchError("request failed (ReadTimeout)")),
], ids=["login-wall", "403", "network"])
def test_unreadable_page_asks_the_owner_to_paste_text(env: Web, monkeypatch: pytest.MonkeyPatch, fetcher: Any) -> None:
    monkeypatch.setattr(reader, "fetch_page", fetcher)
    with pytest.raises(AddJobError) as exc:
        add_job(url="https://www.linkedin.com/jobs/view/123456")
    assert exc.value.needs_text and exc.value.status == 422
    assert "paste the job description text" in str(exc.value) and "LinkedIn" in str(exc.value)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_unsafe_url_is_rejected_with_400(env: Web, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(url: str) -> FetchedPage:
        raise UnsafeURL("The URL points to a private or non-public address")

    monkeypatch.setattr(reader, "fetch_page", refuse)
    with pytest.raises(AddJobError) as exc:
        add_job(url="https://intranet.example/job")
    assert exc.value.status == 400 and not exc.value.needs_text


# --- endpoints ---------------------------------------------------------------------------------------------

@pytest.fixture()
def client(env: Web) -> Iterator[TestClient]:
    with TestClient(app, base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


def test_add_job_endpoint(client: TestClient) -> None:
    resp = client.post("/api/add-job", json={"text": TEXT, "company": "Beta Labs"}, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] and body["job"]["status"] == "read" and body["offer_add_board"] is None
    listed = client.get("/api/jobs").json()
    assert [j["key"] for j in listed] == [body["key"]]


def test_add_job_endpoint_errors(client: TestClient, env: Web, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.post("/api/add-job", json={"text": TEXT}).status_code == 403  # needs the CSRF header
    assert client.post("/api/add-job", json={}, headers=HEADERS).status_code == 422
    assert client.post("/api/add-job", json={"text": "   "}, headers=HEADERS).status_code == 422
    assert client.post("/api/add-job", json={"url": "javascript:alert(1)"}, headers=HEADERS).status_code == 400
    assert client.post("/api/add-job", json={"text": "x" * 100_001}, headers=HEADERS).status_code == 422
    monkeypatch.setattr(reader, "fetch_page", page(fx("page_blocked.html")))
    blocked = client.post("/api/add-job", json={"url": "https://www.indeed.com/viewjob?jk=1"}, headers=HEADERS)
    assert blocked.status_code == 422 and blocked.json()["needs_text"] is True
    assert "paste" in blocked.json()["detail"].lower()


def test_add_board_endpoint(client: TestClient) -> None:
    ok = client.post("/api/add-board", json={"ats": "lever", "slug": "newco", "name": "New Co"}, headers=HEADERS)
    assert ok.json() == {"added": True}
    assert client.post("/api/add-board", json={"ats": "lever", "slug": "NEWCO"}, headers=HEADERS).json() == {"added": False}
    assert load_boards()["lever"] == [Board("newco", "New Co")] and load_boards()["greenhouse"] == [Board("acme", "Acme Inc")]
    for body in ({"ats": "workday", "slug": "x"}, {"ats": "lever", "slug": "../etc"}, {"ats": "lever", "slug": ""}):
        assert client.post("/api/add-board", json=body, headers=HEADERS).status_code == 422


# --- safe fetching (SSRF rules) ------------------------------------------------------------------------------

PUBLIC = "93.184.216.34"


def resolver(mapping: dict[str, list[str]]) -> Callable[[str, int], list[str]]:
    return lambda host, port: mapping.get(host, [PUBLIC])


@pytest.mark.parametrize("address", [
    "127.0.0.1", "127.255.255.254", "10.0.0.5", "172.16.0.1", "172.31.255.255", "192.168.1.1", "169.254.169.254",
    "169.254.0.1", "::1", "fc00::1", "fd12:3456::1", "fe80::1", "0.0.0.0", "100.64.0.1", "224.0.0.1",
    "::ffff:127.0.0.1", "::ffff:10.1.2.3",
])
def test_non_public_addresses_are_blocked(address: str) -> None:
    assert is_public_ip(address) is False


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "172.32.0.1", "2606:2800:220:1:248:1893:25c8:1946"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_ip(address) is True


@pytest.mark.parametrize(("url", "message"), [
    ("http://example.com/job", "https"), ("ftp://example.com/job", "https"), ("https://user:pw@example.com/job", "credentials"),
    ("https://user@example.com/job", "credentials"), ("https://example.com:8443/job", "port"),
    ("https://example.com/a b", "whitespace"), ("https:///nohost", "no host"), ("", "whitespace"),
    ("https://127.0.0.1/admin", "private"), ("https://[::1]/admin", "private"), ("https://169.254.169.254/latest/meta-data", "private"),
    ("https://10.0.0.1/", "private"), ("https://192.168.0.10/x", "private"),
])
def test_validate_url_rejects(url: str, message: str) -> None:
    with pytest.raises(UnsafeURL, match=message):
        validate_url(url, resolver({}))


def test_hostnames_resolving_to_private_addresses_are_blocked() -> None:
    mapping = {"internal.example": ["10.0.0.5"], "mixed.example": [PUBLIC, "192.168.1.1"], "meta.example": ["169.254.169.254"],
               "v6.example": ["fc00::1"], "loop.example": ["127.0.0.1"]}
    for host in mapping:
        with pytest.raises(UnsafeURL, match="private"):
            validate_url(f"https://{host}/job", resolver(mapping))
    assert validate_url("https://good.example/job?x=1", resolver({})) == ("good.example", "/job?x=1", [PUBLIC])


def test_fetch_connects_to_the_validated_ip_and_keeps_host_and_tls_name() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text="<p>Hello é</p>")

    page = fetch_public_page("https://jobs.example/p/1?a=b", resolver=resolver({}), transport=httpx.MockTransport(handler))
    request = seen[0]
    assert request.url.host == PUBLIC and request.headers["host"] == "jobs.example"
    assert request.url.path == "/p/1" and request.url.query == b"a=b"
    assert request.extensions["sni_hostname"] == "jobs.example"
    assert (page.status, page.text, page.url) == (200, "<p>Hello é</p>", "https://jobs.example/p/1?a=b")
    assert request.headers["user-agent"] == "ForgeJobSearch/1.0"


def test_redirects_are_followed_and_every_hop_is_rechecked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "a.example" and request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/moved"})
        if request.headers["host"] in ("a.example", "b.example"):
            return httpx.Response(200, headers={"content-type": "text/html"}, text="ok")
        raise AssertionError("unexpected host")

    mapping = {"a.example": [PUBLIC], "b.example": ["8.8.8.8"]}
    transport = httpx.MockTransport(handler)
    assert fetch_public_page("https://a.example/start", resolver=resolver(mapping), transport=transport).url == "https://a.example/moved"

    def hop_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://b.example/final"}) if request.headers["host"] == "a.example" \
            else httpx.Response(200, headers={"content-type": "text/html"}, text="ok")

    assert fetch_public_page("https://a.example/", resolver=resolver(mapping), transport=httpx.MockTransport(hop_handler)).url == "https://b.example/final"


@pytest.mark.parametrize("location", [
    "https://internal.example/admin", "http://b.example/plain", "https://127.0.0.1/", "https://169.254.169.254/latest/meta-data/",
    "https://user:pw@b.example/", "https://b.example:8443/", "//internal.example/x",
])
def test_redirect_to_an_unsafe_target_is_refused(location: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["host"] == "a.example", "the unsafe hop must never be requested"
        return httpx.Response(302, headers={"location": location})

    mapping = {"a.example": [PUBLIC], "internal.example": ["10.0.0.5"], "b.example": [PUBLIC]}
    with pytest.raises(UnsafeURL):
        fetch_public_page("https://a.example/", resolver=resolver(mapping), transport=httpx.MockTransport(handler))


def test_at_most_three_redirects() -> None:
    hops: list[str] = []

    def endless(request: httpx.Request) -> httpx.Response:
        hops.append(request.url.path)
        return httpx.Response(302, headers={"location": f"/hop{len(hops)}"})

    with pytest.raises(PageFetchError, match="too many redirects"):
        fetch_public_page("https://a.example/", resolver=resolver({}), transport=httpx.MockTransport(endless))
    assert len(hops) == 4  # the original request plus 3 redirects

    count = {"n": 0}

    def three_then_ok(request: httpx.Request) -> httpx.Response:
        count["n"] += 1
        if count["n"] <= 3:
            return httpx.Response(302, headers={"location": f"/hop{count['n']}"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="done")

    assert fetch_public_page("https://a.example/", resolver=resolver({}), transport=httpx.MockTransport(three_then_ok)).text == "done"


def test_response_size_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safe_fetch, "MAX_BYTES", 1000)
    handler = lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=b"a" * 50_000)  # noqa: E731
    page = fetch_public_page("https://a.example/", resolver=resolver({}), transport=httpx.MockTransport(handler))
    assert len(page.text) == 1000 and page.truncated is True


def test_non_html_content_and_bad_redirects_are_errors() -> None:
    pdf = lambda request: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")  # noqa: E731
    with pytest.raises(PageFetchError, match="content type"):
        fetch_public_page("https://a.example/x.pdf", resolver=resolver({}), transport=httpx.MockTransport(pdf))
    bad = lambda request: httpx.Response(302)  # noqa: E731
    with pytest.raises(PageFetchError, match="location"):
        fetch_public_page("https://a.example/", resolver=resolver({}), transport=httpx.MockTransport(bad))


def test_network_errors_and_blocked_statuses_are_reported() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(PageFetchError, match="ConnectError"):
        fetch_public_page("https://a.example/", resolver=resolver({}), transport=httpx.MockTransport(boom))
    forbidden = lambda request: httpx.Response(403, headers={"content-type": "text/html"}, text="no")  # noqa: E731
    assert fetch_public_page("https://a.example/", resolver=resolver({}), transport=httpx.MockTransport(forbidden)).status == 403


def test_second_address_is_tried_when_the_first_refuses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "1.1.1.1":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, headers={"content-type": "text/html"}, text="second")

    page = fetch_public_page("https://a.example/", resolver=resolver({"a.example": ["1.1.1.1", "8.8.8.8"]}),
                             transport=httpx.MockTransport(handler))
    assert page.text == "second"
