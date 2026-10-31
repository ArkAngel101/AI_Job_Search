"""Phase 9: the endpoints and headers the web UI depends on."""
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db, forge
from app.main import app
from app.reader import reader
from app.scout.dedup import MergedListing, aliases
from app.scout.models import RawListing

HEADERS = {"X-Forge-Request": "1"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setitem(forge._state, "active", False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


def store(title: str = "Machine Learning Engineer", description: str = "We use Python.", url: str = "https://example.test/1",
          source: str = "greenhouse", source_id: str = "1") -> str:
    item = RawListing(source=source, source_id=source_id, title=title, company="Acme", location="Boston, MA",
                      description=description, url=url)
    with db.connect() as conn:
        key, _ = db.upsert_seen(conn, MergedListing(listing=item, sources=[source], aliases=aliases(item)), 70, {"mode": "first_cut"}, 1)
    return key


# --- static files and headers ----------------------------------------------------------------------

def test_index_and_static_files_are_served_without_caching(client: TestClient) -> None:
    index = client.get("/")
    assert index.status_code == 200 and index.headers["cache-control"] == "no-store"
    for name in ("core.js", "overview.js", "jobs.js", "cv.js", "settings.js", "app.js", "styles.css"):
        assert f"/static/{name}" in index.text
        resp = client.get(f"/static/{name}")
        assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store" and len(resp.content) > 50
    assert client.get("/static/core.js").headers["content-type"].startswith(("text/javascript", "application/javascript"))


@pytest.mark.parametrize("path", ["/static/../db.py", "/static/%2e%2e/db.py", "/static/..%2fdb.py", "/static/nonexistent.js"])
def test_static_path_traversal_is_refused(client: TestClient, path: str) -> None:
    assert client.get(path).status_code in (400, 404)


def test_security_headers_on_every_response(client: TestClient) -> None:
    for path in ("/", "/static/app.js", "/api/health", "/api/overview"):
        headers = client.get(path).headers
        policy = headers["content-security-policy"]
        assert "default-src 'self'" in policy and "script-src 'self'" in policy and "'unsafe-inline'" not in policy
        assert "'unsafe-eval'" not in policy and "frame-ancestors 'none'" in policy and "object-src 'none'" in policy
        assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "no-referrer"
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400


def test_no_inline_scripts_or_handlers_in_the_page(client: TestClient) -> None:
    html = client.get("/").text.lower()
    assert "<script>" not in html and " onclick=" not in html and "javascript:" not in html


def test_the_ui_code_never_builds_html_from_text() -> None:
    for path in (config.APP_ROOT / "app" / "static").glob("*.js"):
        code = path.read_text()
        assert "innerHTML" not in code and "outerHTML" not in code and "insertAdjacentHTML" not in code and "document.write" not in code, path.name
        assert "eval(" not in code and "new Function" not in code, path.name


# --- agents and activity -------------------------------------------------------------------------------

def test_agents_endpoint(client: TestClient) -> None:
    agents = client.get("/api/agents").json()
    assert [a["name"] for a in agents] == ["forge", "scout", "job-reader", "cv-adapter"]
    assert all(a["status"] == "IDLE" and a["tasks"] == 0 and a["success_rate"] is None and a["last_action"] is None for a in agents)
    db.log_activity("scout", "Scanned 3 sources", "completed")
    db.log_activity("scout", "Scan failed: X", "failed")
    db.log_activity("cv-adapter", "Structure CV", "completed", "claude-sonnet-5-5")
    by_name = {a["name"]: a for a in client.get("/api/agents").json()}
    assert by_name["scout"]["tasks"] == 2 and by_name["scout"]["success_rate"] == 50 and by_name["scout"]["last_action"] == "Scan failed: X"
    assert by_name["scout"]["status"] == "LIVE" and by_name["scout"]["last_model"] == "none"
    assert by_name["cv-adapter"]["last_model"] == "claude-sonnet-5-5" and by_name["forge"]["status"] == "IDLE"


def test_agent_status_is_driven_by_backend_state(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    db.log_activity("forge", "Find run #1 started", "completed")
    monkeypatch.setitem(forge._state, "active", True)
    by_name = {a["name"]: a["status"] for a in client.get("/api/agents").json()}
    assert by_name == {"forge": "WORKING", "scout": "WORKING", "job-reader": "IDLE", "cv-adapter": "IDLE"}
    monkeypatch.setitem(forge._state, "active", False)
    monkeypatch.setattr(reader, "_busy", 1)
    assert {a["name"]: a["status"] for a in client.get("/api/agents").json()}["job-reader"] == "WORKING"
    monkeypatch.setattr(reader, "_busy", 0)


def test_old_activity_is_idle_not_live(client: TestClient) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO activity_log (id, agent_name, task_description, status, model_used, created_at) VALUES ('a', 'scout', 'old', 'completed', 'none', '2020-01-01T00:00:00+00:00')")
    assert {a["name"]: a["status"] for a in client.get("/api/agents").json()}["scout"] == "IDLE"


def test_activity_endpoint(client: TestClient) -> None:
    for i in range(5):
        db.log_activity("forge", f"step {i}", "completed")
    rows = client.get("/api/activity", params={"limit": 3}).json()
    assert [r["task"] for r in rows] == ["step 4", "step 3", "step 2"]
    assert set(rows[0]) == {"id", "agent", "task", "status", "model", "at"}
    assert len(client.get("/api/activity").json()) == 5
    for bad in (0, 201, "x"):
        assert client.get("/api/activity", params={"limit": bad}).status_code == 422


def test_attributions_endpoint(client: TestClient) -> None:
    credits = client.get("/api/attributions").json()
    assert credits["remoteok"]["url"] == "https://remoteok.com" and credits["themuse"]["url"] == "https://www.themuse.com"


# --- job status, job detail, safe links ---------------------------------------------------------------------

def test_set_job_status(client: TestClient) -> None:
    key = store()
    for status in ("interested", "applied", "interview", "rejected", "new"):
        resp = client.post("/api/job/status", json={"key": key, "user_status": status}, headers=HEADERS)
        assert resp.status_code == 200 and resp.json() == {"key": key, "user_status": status}
        assert client.get("/api/jobs").json()[0]["user_status"] == status
    assert client.post("/api/job/status", json={"key": key, "user_status": "hired"}, headers=HEADERS).status_code == 422
    assert client.post("/api/job/status", json={"key": "nope", "user_status": "new"}, headers=HEADERS).status_code == 404
    assert client.post("/api/job/status", json={"key": key, "user_status": "new"}).status_code == 403


def test_job_detail_before_and_after_reading(client: TestClient) -> None:
    key = store(description="Requirements\n- Python and SQL\nWe are unable to provide visa sponsorship.")
    before = client.get("/api/jd", params={"key": key}).json()
    assert before["read"] is False and before["jd"] is None and "Python and SQL" in before["description"]
    reader.read_job(key)
    after = client.get("/api/jd", params={"key": key}).json()
    assert after["read"] is True and after["jd"]["required_skills"] == ["Python", "SQL"] and after["jd"]["flags"]["no_sponsorship"]
    assert after["score_breakdown"]["mode"] == "final" and after["status"] == "read"
    assert client.get("/api/jd", params={"key": "nope"}).status_code == 404


def test_sponsorship_warning_flag_appears_on_the_job_row(client: TestClient) -> None:
    flagged = store(description="Requirements\n- Python\nWe cannot offer visa sponsorship.", source_id="1", title="Machine Learning Engineer")
    clean = store(description="Requirements\n- Python\nVisa sponsorship is available.", source_id="2", title="Data Scientist", url="https://example.test/2")
    rows = {r["key"]: r for r in client.get("/api/jobs").json()}
    assert rows[flagged]["needs_sponsorship"] is False and rows[clean]["needs_sponsorship"] is False  # not read yet
    reader.read_job(flagged)
    reader.read_job(clean)
    rows = {r["key"]: r for r in client.get("/api/jobs").json()}
    assert rows[flagged]["needs_sponsorship"] is True and rows[clean]["needs_sponsorship"] is False


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,<script>1</script>", "vbscript:x", "//evil.example", ""])
def test_unsafe_links_never_reach_the_ui(client: TestClient, url: str) -> None:
    key = store(url=url)
    assert client.get("/api/jobs").json()[0]["url"] == ""
    assert client.get("/api/jd", params={"key": key}).json()["url"] == ""
    assert db.safe_url("https://ok.example/x") == "https://ok.example/x" and db.safe_url("HTTP://ok.example") == "HTTP://ok.example"
