"""Phase 5: the Find run (Forge), job memory, and the run/jobs/overview endpoints."""
import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, db, forge
from app.main import app
from app.profile import Profile
from app.scout.http import ScoutClient

FIXTURES = Path(__file__).parent / "fixtures"
HEADERS = {"X-Forge-Request": "1"}


def fx(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class Web:
    """Mock internet: one response per host, changeable between runs."""

    def __init__(self) -> None:
        self.responses: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.requests: list[httpx.Request] = []
        self.gate: threading.Event | None = None  # when set, requests block until released

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.gate is not None:
            self.gate.wait(10)
        return self.responses[request.url.host](request)

    def serve(self, host: str, data: Any, status: int = 200) -> None:
        self.responses[host] = lambda request: httpx.Response(status, json=data)

    def reset_defaults(self) -> None:
        self.serve("boards-api.greenhouse.io", fx("greenhouse.json"))
        self.serve("api.lever.co", fx("lever.json"))
        self.serve("remoteok.com", fx("remoteok.json"))


@pytest.fixture()
def web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Web]:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    db.init_db()
    (config.DATA_DIR / "boards.yaml").write_text("greenhouse:\n  - {slug: acme, name: Acme}\nlever:\n  - {slug: beta, name: Beta}\n")
    web = Web()
    web.reset_defaults()
    monkeypatch.setattr(
        forge, "make_client", lambda: ScoutClient(transport=httpx.MockTransport(web.handler), sleep=lambda s: None)
    )
    monkeypatch.setitem(forge._state, "active", False)
    monkeypatch.setitem(forge._state, "sources", {})
    save_profile(enabled_sources=["greenhouse", "lever", "remoteok"], max_age_days=100000)
    yield web
    if web.gate is not None:
        web.gate.set()
    forge.wait_for_run(10)


def save_profile(**changes: Any) -> None:
    with db.connect() as conn:
        stored = db.get_setting(conn, "profile", {})
        db.set_setting(conn, "profile", {**stored, **changes})


def jobs_by_title() -> dict[str, dict[str, Any]]:
    with db.connect() as conn:
        return {r["title"]: dict(r) for r in conn.execute("SELECT * FROM jobs")}


def run_once() -> dict[str, Any]:
    run_id = forge.start_find(background=False)
    with db.connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "completed", run["summary_json"]
    return json.loads(run["summary_json"])


# --- the spec's "Done when" ----------------------------------------------------

def test_scanning_twice_increments_times_seen_and_new_drops(web: Web) -> None:
    first = run_once()
    jobs = jobs_by_title()
    assert set(jobs) == {"Machine Learning Engineer, New Grad", "Data Scientist, Intern", "NLP Engineer", "Data Scientist"}
    assert first["new"] == 4 and first["found"] == 6 and first["matched"] == 4
    assert all(j["times_seen"] == 1 and j["status"] == "matched" for j in jobs.values())

    second = run_once()
    jobs_again = jobs_by_title()
    assert second["new"] == 0 and second["matched"] == 4
    assert all(j["times_seen"] == 2 for j in jobs_again.values())
    assert {j["key"] for j in jobs_again.values()} == {j["key"] for j in jobs.values()}  # no duplicate keys
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 4
        assert db.overview_counts(conn)["new"] == 0
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


def test_source_failure_is_noted_without_stopping_the_run(web: Web) -> None:
    web.serve("remoteok.com", {}, status=500)
    summary = run_once()
    assert any("remoteok: failed (HTTP 500)" in n for n in summary["notes"])
    assert summary["sources"]["remoteok"] == "failed" and summary["sources"]["greenhouse"] == "done"
    assert set(jobs_by_title()) == {"Machine Learning Engineer, New Grad", "Data Scientist, Intern"}


def test_no_llm_is_called(web: Web, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.adapter import llm

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("an LLM client was created during a Find run")

    monkeypatch.setattr(llm, "GroqClient", forbidden)
    run_once()
    assert not [r for r in web.requests if "groq" in r.url.host]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM activity_log WHERE agent_name = 'cv-adapter'").fetchone()[0] == 0


# --- closed and reopened ---------------------------------------------------------

def test_job_missing_from_a_complete_board_is_closed_then_reopened(web: Web) -> None:
    run_once()
    only_sales = {"jobs": [fx("greenhouse.json")["jobs"][1]], "meta": {"total": 1}}
    web.serve("boards-api.greenhouse.io", only_sales)
    summary = run_once()
    assert summary["closed"] == 1 and jobs_by_title()["Machine Learning Engineer, New Grad"]["closed"] == 1
    assert jobs_by_title()["Data Scientist, Intern"]["closed"] == 0  # other board untouched

    web.reset_defaults()
    summary = run_once()
    assert summary["reopened"] == 1 and jobs_by_title()["Machine Learning Engineer, New Grad"]["closed"] == 0


def test_failed_board_fetch_closes_nothing(web: Web) -> None:
    run_once()
    web.serve("boards-api.greenhouse.io", {}, status=500)
    summary = run_once()
    assert summary["closed"] == 0 and all(j["closed"] == 0 for j in jobs_by_title().values())


# --- filters, caps, and merging ----------------------------------------------------

def test_max_age_filter_drops_old_listings(web: Web) -> None:
    save_profile(max_age_days=1)
    summary = run_once()
    assert summary["found"] == 6 and summary["eligible"] < 6
    assert "Machine Learning Engineer, New Grad" not in jobs_by_title()  # 2026-10-20 is long past 1 day


def test_non_us_listings_are_not_stored(web: Web) -> None:
    data = fx("remoteok.json")
    data[1]["location"] = "London, UK"
    web.serve("remoteok.com", data)
    run_once()
    assert "NLP Engineer" not in jobs_by_title() and "Data Scientist" in jobs_by_title()


def test_results_per_scan_caps_stored_jobs(web: Web) -> None:
    save_profile(results_per_scan=2)
    summary = run_once()
    assert summary["matched"] == 4 and summary["stored"] == 2 and len(jobs_by_title()) == 2


def test_first_cut_threshold_comes_from_the_profile(web: Web) -> None:
    save_profile(first_cut=99)
    assert run_once()["matched"] == 0 and jobs_by_title() == {}


def test_better_listing_from_an_ats_replaces_the_aggregator_listing(web: Web) -> None:
    save_profile(enabled_sources=["remoteok"])
    web.serve("remoteok.com", [
        {"legal": "notice"},
        {"id": "r1", "position": "Machine Learning Engineer, New Grad", "company": "Acme", "location": "San Francisco, CA",
         "description": "short", "url": "https://remoteok.com/r1", "date": "2026-10-30T10:00:00+00:00"}])
    run_once()
    before = jobs_by_title()["Machine Learning Engineer, New Grad"]
    assert before["source"] == "remoteok"

    save_profile(enabled_sources=["remoteok", "greenhouse"])
    run_once()
    after = jobs_by_title()["Machine Learning Engineer, New Grad"]
    assert after["key"] == before["key"] and after["source"] == "greenhouse" and after["times_seen"] == 2
    assert after["board"] == "greenhouse/acme"
    with db.connect() as conn:
        assert db.sources_seen(conn, after["key"]) == ["greenhouse", "remoteok"]
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_status_never_downgrades_on_rescan(web: Web) -> None:
    run_once()
    key = jobs_by_title()["Data Scientist"]["key"]
    with db.connect() as conn:
        db.advance_status(conn, key, "read")
        conn.execute("UPDATE jobs SET score = 99 WHERE key = ?", (key,))
    run_once()
    row = jobs_by_title()["Data Scientist"]
    assert row["status"] == "read" and row["score"] == 99  # final-mode score kept


# --- source frequency limits ----------------------------------------------------------

def test_remotive_is_skipped_when_fetched_recently(web: Web) -> None:
    web.serve("remotive.com", fx("remotive.json"))
    save_profile(enabled_sources=["remotive"])
    first = run_once()
    assert first["found"] == 2 and not first["notes"]
    second = run_once()
    assert second["found"] == 0 and any("remotive: skipped" in n for n in second["notes"])
    assert second["sources"]["remotive"] == "skipped"
    assert len([r for r in web.requests if r.url.host == "remotive.com"]) == 1


# --- activity log and stale runs ---------------------------------------------------------

def test_activity_is_logged_by_code(web: Web) -> None:
    run_once()
    with db.connect() as conn:
        rows = conn.execute("SELECT agent_name, task_description, status, model_used FROM activity_log ORDER BY created_at").fetchall()
    agents = [r["agent_name"] for r in rows]
    assert agents == ["forge", "scout", "forge"]
    assert rows[0]["task_description"].endswith("started") and rows[2]["task_description"].endswith("finished")
    assert "4 matched" in rows[1]["task_description"] and all(r["model_used"] == "none" for r in rows)


def test_crash_closes_the_run_as_failed(web: Web, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(forge, "fetch_all", boom)
    forge.start_find(background=False)
    with db.connect() as conn:
        run = db.latest_run(conn)
        failed = conn.execute("SELECT task_description FROM activity_log WHERE status = 'failed'").fetchall()
    assert run and run["status"] == "failed" and run["summary"] == {"error": "RuntimeError"}
    assert [r[0] for r in failed] == ["Scan failed: RuntimeError", "Find run #1 failed"]
    assert forge.get_status()["active"] is False


def test_stale_running_runs_fail_on_startup(web: Web) -> None:
    with db.connect() as conn:
        run_id = db.create_run(conn, "button")
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        status = client.get("/api/run-status").json()
    assert status["run"]["id"] == run_id and status["run"]["status"] == "failed"
    assert "interrupted" in status["run"]["summary"]["error"]


# --- endpoints ----------------------------------------------------------------------------

@pytest.fixture()
def client(web: Web) -> Iterator[TestClient]:
    with TestClient(app, base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


def test_run_endpoint_rejects_a_second_run_with_409_and_reports_progress(web: Web, client: TestClient) -> None:
    web.gate = threading.Event()
    resp = client.post("/api/run", headers=HEADERS)
    assert resp.status_code == 202 and resp.json() == {"run_id": 1}

    again = client.post("/api/run", headers=HEADERS)
    assert again.status_code == 409

    for _ in range(200):  # the background thread needs a moment to start its first source
        status = client.get("/api/run-status").json()  # a "page reload" mid-run still sees live state
        if "running" in status["sources"].values():
            break
        time.sleep(0.01)
    assert status["active"] is True and status["run"]["status"] == "running"
    assert set(status["sources"]) == {"greenhouse", "lever", "remoteok"}
    assert "running" in status["sources"].values()

    web.gate.set()
    forge.wait_for_run(10)
    done = client.get("/api/run-status").json()
    assert done["active"] is False and done["run"]["status"] == "completed"
    assert done["sources"] == {"greenhouse": "done", "lever": "done", "remoteok": "done"}
    assert done["run"]["summary"]["matched"] == 4


def test_run_endpoint_needs_the_csrf_header(client: TestClient) -> None:
    assert client.post("/api/run").status_code == 403


def test_jobs_endpoint_shape_ranking_and_filters(web: Web, client: TestClient) -> None:
    client.post("/api/run", headers=HEADERS)
    forge.wait_for_run(10)
    rows = client.get("/api/jobs").json()
    assert [r["rank"] for r in rows] == [1, 2, 3, 4]
    assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)
    assert set(rows[0]) == {"rank", "score", "title", "company", "location", "url", "source", "new", "times_seen",
                            "closed", "status", "user_status", "key", "needs_sponsorship"}
    assert all(r["new"] and r["status"] == "matched" and r["user_status"] == "new" and r["closed"] is False for r in rows)

    assert {r["source"] for r in client.get("/api/jobs", params={"source": "lever"}).json()} == {"lever"}
    assert len(client.get("/api/jobs", params={"q": "scientist"}).json()) == 2
    assert client.get("/api/jobs", params={"q": "100%"}).json() == []  # wildcards are escaped
    assert len(client.get("/api/jobs", params={"limit": 1, "offset": 2}).json()) == 1
    assert client.get("/api/jobs", params={"limit": 1, "offset": 2}).json()[0]["rank"] == 3
    assert client.get("/api/jobs", params={"min_score": 100}).json() == []
    assert len(client.get("/api/jobs", params={"new_only": "true"}).json()) == 4

    client.post("/api/run", headers=HEADERS)
    forge.wait_for_run(10)
    assert client.get("/api/jobs", params={"new_only": "true"}).json() == []  # nothing new on the second run
    assert all(r["times_seen"] == 2 and not r["new"] for r in client.get("/api/jobs").json())


def test_jobs_endpoint_hide_closed(web: Web, client: TestClient) -> None:
    client.post("/api/run", headers=HEADERS)
    forge.wait_for_run(10)
    web.serve("boards-api.greenhouse.io", {"jobs": [], "meta": {"total": 0}})
    client.post("/api/run", headers=HEADERS)
    forge.wait_for_run(10)
    assert len(client.get("/api/jobs").json()) == 4
    visible = client.get("/api/jobs", params={"hide_closed": "true"}).json()
    assert len(visible) == 3 and all(not r["closed"] for r in visible)


@pytest.mark.parametrize("params", [
    {"status": "bogus"}, {"user_status": "hired"}, {"min_score": 101}, {"limit": 0}, {"limit": 5000}, {"offset": -1},
    {"min_score": "abc"},
])
def test_jobs_endpoint_rejects_bad_input(client: TestClient, params: dict[str, Any]) -> None:
    assert client.get("/api/jobs", params=params).status_code == 422


def test_overview_counts(web: Web, client: TestClient) -> None:
    assert client.get("/api/overview").json() == {"found": 0, "matched": 0, "read": 0, "tailored": 0, "known": 0, "new": 0}
    client.post("/api/run", headers=HEADERS)
    forge.wait_for_run(10)
    with db.connect() as conn:
        keys = [r["key"] for r in conn.execute("SELECT key FROM jobs ORDER BY score DESC")]
        db.advance_status(conn, keys[0], "read")
        db.advance_status(conn, keys[1], "tailored")
    assert client.get("/api/overview").json() == {"found": 6, "matched": 4, "read": 2, "tailored": 1, "known": 4, "new": 4}


def test_profile_defaults_load_without_saved_settings(web: Web) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM settings")
    assert forge.load_profile() == Profile()
    save_profile(seniority="bogus")  # invalid saved values fall back to defaults rather than crashing a scan
    assert forge.load_profile() == Profile()
