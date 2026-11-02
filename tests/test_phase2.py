"""Phase 2: Scout source modules, using fixtures only (no live network)."""
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app import config
from app.profile import Profile
from app.scout.attributions import ATTRIBUTIONS
from app.scout.boards import Board, ensure_boards_file, load_boards, parse_boards
from app.scout.http import USER_AGENT, ScoutClient
from app.scout.models import RawListing
from app.scout.sources import SOURCES, adzuna, ashby, fetch_all, greenhouse, jobicy, lever
from app.scout.sources import remoteok, remotive, themuse, workable
from app.scout.text import strip_html

FIXTURES = Path(__file__).parent / "fixtures"
Handler = Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def ok(name: str) -> httpx.Response:
    return httpx.Response(200, json=fixture(name))


class FakeTime:
    """Deterministic clock: sleeping advances time instantly and is recorded."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


@pytest.fixture()
def fake_time() -> FakeTime:
    return FakeTime()


def make_client(handler: Handler, fake_time: FakeTime) -> ScoutClient:
    return ScoutClient(transport=httpx.MockTransport(handler), sleep=fake_time.sleep, clock=fake_time.clock)


def b(slug: str, name: str | None = None) -> Board:
    return Board(slug=slug, name=name or slug.title())


PROFILE = Profile(keywords=["machine learning"], pages=2)


# --- text and client ---------------------------------------------------------

def test_strip_html_keeps_structure_and_drops_scripts() -> None:
    text = strip_html("<p>Hello&nbsp;<b>world</b></p><script>x()</script><ul><li>One</li><li>Two</li></ul>a<br>b")
    assert text == "Hello world\n- One\n- Two\n\na\nb"
    assert strip_html(None) == ""


def test_client_sends_descriptive_user_agent(fake_time: FakeTime) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, json={})

    make_client(handler, fake_time).get("https://example.test/")
    assert seen == [USER_AGENT] and USER_AGENT == "ForgeJobSearch/1.0"


# --- ATS sources -------------------------------------------------------------

def test_greenhouse_normalizes_and_decodes_content(fake_time: FakeTime) -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return ok("greenhouse.json")

    listings, notes = greenhouse.fetch(PROFILE, {"greenhouse": [b("acme", "Acme Inc")]}, make_client(handler, fake_time))
    assert urls == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"]
    first = listings[0]
    assert (first.source, first.source_id, first.title, first.company) == (
        "greenhouse", "4001", "Machine Learning Engineer, New Grad", "Acme Inc")
    assert first.location == "San Francisco, CA"
    assert first.url == "https://boards.greenhouse.io/acme/jobs/4001"
    assert first.description == "Build ML systems & pipelines.\n- Python\n- PyTorch"
    assert first.created == "2026-10-20T10:00:00-04:00"
    assert first.board == "greenhouse/acme"
    assert notes.completed_boards == {"greenhouse/acme"}


def test_greenhouse_incomplete_response_is_not_marked_complete(fake_time: FakeTime) -> None:
    listings, notes = greenhouse.fetch(
        PROFILE, {"greenhouse": [b("acme")]}, make_client(lambda r: ok("greenhouse_incomplete.json"), fake_time))
    assert listings == [] and notes.completed_boards == set()
    assert "incomplete" in notes[0]


def test_lever_normalizes_and_spaces_requests(fake_time: FakeTime) -> None:
    client = make_client(lambda r: ok("lever.json"), fake_time)
    listings, notes = lever.fetch(PROFILE, {"lever": [b("acme"), b("beta")]}, client)
    first = listings[0]
    assert (first.source_id, first.title, first.url) == ("lv-1", "Data Scientist, Intern", "https://jobs.lever.co/acme/lv-1")
    assert first.location == "New York, NY, Remote"
    assert (first.salary_min, first.salary_max, first.contract_type) == (90000.0, 120000.0, "Intern")
    assert first.created is not None and first.created.startswith("2026-")
    assert "Analyze data." in first.description and "Requirements\n- SQL\n- Python" in first.description
    assert listings[1].salary_min is None
    assert len(listings) == 4  # two boards, two postings each
    assert fake_time.sleeps == [1.0]  # at least 1 second between Lever requests
    assert notes.completed_boards == {"lever/acme", "lever/beta"}


def test_ashby_normalizes_and_skips_unlisted(fake_time: FakeTime) -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return ok("ashby.json")

    listings, _ = ashby.fetch(PROFILE, {"ashby": [b("acme", "Acme")]}, make_client(handler, fake_time))
    assert urls == ["https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true"]
    assert [item.source_id for item in listings] == ["ab-1"]
    job = listings[0]
    assert (job.title, job.location, job.salary_min, job.salary_max) == ("AI Engineer", "San Francisco, Remote", 150000.0, 200000.0)
    assert job.created == "2026-10-15T12:00:00.000+00:00" and job.description == "Build LLM apps."


def test_workable_normalizes_and_spaces_requests(fake_time: FakeTime) -> None:
    client = make_client(lambda r: ok("workable.json"), fake_time)
    listings, _ = workable.fetch(PROFILE, {"workable": [b("acme"), b("beta")]}, client)
    job = listings[0]
    assert (job.source_id, job.company, job.url) == ("AAA111", "Acme Labs", "https://apply.workable.com/j/AAA111")
    assert job.location == "Austin, Texas, United States, Remote"
    assert job.description == "Work on open source." and job.contract_type == "Full-time"
    assert fake_time.sleeps == [3.0]  # about 3 seconds between Workable requests


def test_missing_board_404_is_skipped_and_noted(fake_time: FakeTime) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "nope"}) if "ghost" in request.url.path else ok("greenhouse.json")

    boards = {"greenhouse": [b("ghost"), b("acme")]}
    listings, notes = greenhouse.fetch(PROFILE, boards, make_client(handler, fake_time))
    assert len(listings) == 2
    assert any("greenhouse/ghost" in n and "404" in n for n in notes)
    assert notes.completed_boards == {"greenhouse/acme"}


def test_server_error_and_bad_shape_are_noted_not_fatal(fake_time: FakeTime) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "bad500" in request.url.path:
            return httpx.Response(500)
        if "weird" in request.url.path:
            return httpx.Response(200, json={"unexpected": True})
        return ok("greenhouse.json")

    boards = {"greenhouse": [b("bad500"), b("weird"), b("acme")]}
    listings, notes = greenhouse.fetch(PROFILE, boards, make_client(handler, fake_time))
    assert len(listings) == 2
    assert any("HTTP 500" in n for n in notes) and any("unexpected response shape" in n for n in notes)
    assert notes.completed_boards == {"greenhouse/acme"}


def test_network_error_is_noted_without_url(fake_time: FakeTime) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom https://secret.example/?key=abc", request=request)

    _, notes = lever.fetch(PROFILE, {"lever": [b("acme")]}, make_client(handler, fake_time))
    assert notes and "secret" not in " ".join(notes) and "ConnectError" in notes[0]


# --- aggregator sources ------------------------------------------------------

def test_adzuna_skipped_without_keys(monkeypatch: pytest.MonkeyPatch, fake_time: FakeTime) -> None:
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    calls: list[httpx.Request] = []
    listings, notes = adzuna.fetch(PROFILE, {}, make_client(lambda r: calls.append(r) or ok("adzuna.json"), fake_time))
    assert listings == [] and calls == []
    assert "ADZUNA_APP_ID" in notes[0]


def test_adzuna_pages_and_relevance_requests(monkeypatch: pytest.MonkeyPatch, fake_time: FakeTime) -> None:
    monkeypatch.setenv("ADZUNA_APP_ID", "id123")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key456")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return ok("adzuna.json")

    listings, notes = adzuna.fetch(Profile(keywords=["machine learning"], pages=2), {}, make_client(handler, fake_time))
    paths = [r.url.path.rsplit("/", 1)[-1] for r in requests]
    sorts = [r.url.params.get("sort_by") for r in requests]
    assert paths == ["1", "2", "1"] and sorts == ["date", "date", None]  # newest pages 1-2, then relevance page 1
    assert all(r.url.params["what"] == "machine learning" for r in requests)
    assert [item.source_id for item in listings] == ["9001", "9002"]  # de-duplicated within the source
    job = listings[0]
    assert (job.company, job.location, job.salary_min, job.contract_type) == ("Acme", "Boston, Massachusetts", 110000.0, "permanent")
    assert job.description == "Train models & ship them..." and notes == []
    assert job.url == "https://www.adzuna.com/land/ad/9001?se=x&utm_medium=api&v=ABC"  # the app id (utm_source) is removed
    assert "id123" not in " ".join(item.url for item in listings)


def test_adzuna_drops_trailing_words_until_results(monkeypatch: pytest.MonkeyPatch, fake_time: FakeTime) -> None:
    monkeypatch.setenv("ADZUNA_APP_ID", "id123")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key456")

    def handler(request: httpx.Request) -> httpx.Response:
        what = request.url.params["what"]
        return ok("adzuna.json") if what == "applied" else ok("adzuna_empty.json")

    profile = Profile(keywords=["applied scientist intern"], pages=1)
    listings, notes = adzuna.fetch(profile, {}, make_client(handler, fake_time))
    assert len(listings) == 2
    assert notes == ["adzuna: 'applied scientist intern' had no results, searched 'applied' instead"]


def test_adzuna_errors_never_leak_keys(monkeypatch: pytest.MonkeyPatch, fake_time: FakeTime) -> None:
    monkeypatch.setenv("ADZUNA_APP_ID", "id123")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key456")
    listings, notes = adzuna.fetch(
        Profile(keywords=["a", "b"], pages=1), {}, make_client(lambda r: httpx.Response(401), fake_time))
    text = " ".join(notes)
    assert listings == [] and "key456" not in text and "id123" not in text
    assert "credentials rejected" in text


def test_remotive_makes_one_request(fake_time: FakeTime) -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return ok("remotive.json")

    listings, _ = remotive.fetch(PROFILE, {}, make_client(handler, fake_time))
    assert urls == ["https://remotive.com/api/remote-jobs"]
    assert remotive.MIN_SCAN_GAP_SECONDS >= 6 * 3600
    job = listings[0]
    assert (job.source_id, job.company, job.location, job.contract_type) == ("7001", "Remo Co", "USA Only", "full_time")
    assert job.description == "Build models." and job.url.startswith("https://remotive.com/")
    assert listings[1].location == "Remote" and listings[1].contract_type is None


def test_remoteok_skips_legal_notice(fake_time: FakeTime) -> None:
    listings, _ = remoteok.fetch(PROFILE, {}, make_client(lambda r: ok("remoteok.json"), fake_time))
    assert [item.source_id for item in listings] == ["1137607", "1137608"]
    assert (listings[0].salary_min, listings[1].salary_min) == (None, 100000.0)  # 0 means not listed
    assert listings[0].title == "NLP Engineer" and listings[0].description == "Work on NLP."


def test_themuse_paginates_per_category_and_stops_on_empty(fake_time: FakeTime) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return ok("themuse_page1.json") if request.url.params["page"] == "1" else ok("themuse_empty.json")

    listings, notes = themuse.fetch(Profile(pages=3), {}, make_client(handler, fake_time))
    assert [(r.url.params["category"], r.url.params["page"]) for r in requests] == [
        ("Data Science", "1"), ("Data Science", "2"), ("Software Engineering", "1"), ("Software Engineering", "2")]
    assert [item.source_id for item in listings] == ["5001", "5002"]  # same ids across categories collapse
    job = listings[0]
    assert (job.company, job.location, job.url) == (
        "Acme", "New York, NY; Flexible / Remote", "https://www.themuse.com/jobs/acme/data-scientist")
    assert job.description == "Analyze data." and notes == []


def test_jobicy_follows_cursor_and_drops_non_yearly_salary(fake_time: FakeTime) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return ok("jobicy_page2.json") if request.url.params.get("cursor") == "abc123==" else ok("jobicy_page1.json")

    listings, _ = jobicy.fetch(Profile(pages=5), {}, make_client(handler, fake_time))
    assert len(requests) == 2 and requests[0].url.params["geo"] == "usa"
    assert "cursor" not in requests[0].url.params
    first, second = listings
    assert (first.salary_min, first.salary_max, first.contract_type) == (100000.0, 130000.0, "Full-Time")
    assert (second.salary_min, second.location) == (None, "Remote, Anywhere")
    assert jobicy.MIN_SCAN_GAP_SECONDS == 3600


# --- runner and registry -----------------------------------------------------

def test_failing_source_never_aborts_the_run(monkeypatch: pytest.MonkeyPatch, fake_time: FakeTime) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "remotive.com":
            return httpx.Response(500)
        return ok("remoteok.json")

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret https://x/?key=abc")

    monkeypatch.setattr(SOURCES["jobicy"], "fetch", explode)
    progress: list[tuple[str, str]] = []
    listings, notes = fetch_all(
        ["remotive", "jobicy", "nonesuch", "remoteok"], PROFILE, {}, make_client(handler, fake_time),
        on_progress=lambda name, state: progress.append((name, state)))
    assert [item.source for item in listings] == ["remoteok", "remoteok"]
    text = " ".join(notes)
    assert "remotive: failed (HTTP 500)" in text and "jobicy: failed (RuntimeError)" in text
    assert "nonesuch: unknown source" in text and "secret" not in text
    assert progress == [
        ("remotive", "running"), ("remotive", "failed"), ("jobicy", "running"), ("jobicy", "failed"),
        ("remoteok", "running"), ("remoteok", "done")]


def test_fetch_all_collects_completed_boards(fake_time: FakeTime) -> None:
    boards = {"greenhouse": [b("acme")], "lever": [b("beta")]}

    def handler(request: httpx.Request) -> httpx.Response:
        return ok("lever.json") if request.url.host == "api.lever.co" else ok("greenhouse.json")

    listings, notes = fetch_all(["greenhouse", "lever"], PROFILE, boards, make_client(handler, fake_time))
    assert notes.completed_boards == {"greenhouse/acme", "lever/beta"}
    assert all(isinstance(item, RawListing) for item in listings)


def test_every_enabled_source_has_a_module() -> None:
    assert set(Profile().enabled_sources) == set(SOURCES)


def test_attributions_cover_required_sources() -> None:
    assert ATTRIBUTIONS["remoteok"]["url"] == "https://remoteok.com"
    assert ATTRIBUTIONS["themuse"]["url"] == "https://www.themuse.com"


# --- boards.yaml -------------------------------------------------------------

def test_parse_boards_accepts_slugs_and_names() -> None:
    boards = parse_boards("greenhouse:\n  - acme\n  - {slug: beta, name: Beta Corp}\nlever: []\n")
    assert boards["greenhouse"] == [Board("acme", "acme"), Board("beta", "Beta Corp")]
    assert boards["lever"] == []


@pytest.mark.parametrize("text", [
    "greenhouse:\n  - '../etc/passwd'\n", "greenhouse:\n  - 'a b'\n", "workday:\n  - acme\n", "- just\n- a list\n",
])
def test_parse_boards_rejects_bad_input(text: str) -> None:
    with pytest.raises(ValueError):
        parse_boards(text)


def test_default_boards_file_is_valid_and_copied_to_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    path = ensure_boards_file()
    assert path == tmp_path / "data" / "boards.yaml" and path.exists()
    path.write_text("lever:\n  - mine\n")
    assert ensure_boards_file() == path and load_boards() == {"lever": [Board("mine", "mine")]}  # not overwritten
    default = parse_boards((config.APP_ROOT / "boards.yaml").read_text())
    assert [x.slug for x in default["greenhouse"]] == ["anthropic", "databricks", "stripe", "scaleai"]
    assert [x.slug for x in default["workable"]] == ["huggingface"]
