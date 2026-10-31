"""Phase 11: the API audit script, run in-process (the same script can be pointed at the live container)."""
import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import config, db, forge
from app.main import app
from app.scout.dedup import MergedListing, aliases
from app.scout.models import RawListing

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "api_audit.py"
spec = importlib.util.spec_from_file_location("api_audit", SCRIPT)
api_audit = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(api_audit)  # type: ignore[union-attr]


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setitem(forge._state, "active", False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


def seed_job() -> None:
    item = RawListing(source="greenhouse", source_id="1", title="Machine Learning Engineer", company="Acme", location="Boston, MA",
                      description="Requirements\n- Python", url="https://example.test/1")
    with db.connect() as conn:
        db.upsert_seen(conn, MergedListing(listing=item, sources=["greenhouse"], aliases=aliases(item)), 80, {"mode": "first_cut"}, 1)


def test_the_audit_passes_on_an_empty_install(client: TestClient) -> None:
    result = api_audit.audit(client)
    assert result.failures == [] and result.checks > 100


def test_the_audit_passes_with_a_job_and_customized_settings(client: TestClient) -> None:
    seed_job()
    client.post("/api/settings", json={"profile": {"keywords": ["robotics"], "seniority": "mid"}, "claude": {"monthly_cap_usd": 5}},
                headers=api_audit.WRITE)
    client.post("/api/boards", json={"yaml": "# mine\nlever:\n  - {slug: beta, name: Beta}\n"}, headers=api_audit.WRITE)
    before = client.get("/api/settings").json()
    result = api_audit.audit(client)
    assert result.failures == []
    after = client.get("/api/settings").json()
    # the audit's write tests are undone: profile snapshot and restore
    assert after["profile"] == before["profile"] and after["customized"] == before["customized"]
    assert after["claude"] == before["claude"] and after["boards_yaml"].strip() == before["boards_yaml"].strip()
    jobs = client.get("/api/jobs").json()
    assert len(jobs) == 1 and jobs[0]["user_status"] == "new"


def test_the_audit_covers_every_api_route(client: TestClient) -> None:
    result = api_audit.audit(client)
    routes = {
        (method, route.path)
        for route in app.routes if isinstance(route, APIRoute)
        for method in route.methods - {"HEAD", "OPTIONS"}
    }
    missing = {pair for pair in routes if pair not in result.covered}
    assert missing == set(), f"add these endpoints to scripts/api_audit.py: {sorted(missing)}"


def test_the_audit_notices_problems(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A guard against an audit that can never fail: switch off write protection and expect failures."""
    from app import security

    started: list[str] = []
    monkeypatch.setattr(forge, "start_find", lambda *a, **k: started.append("scan") or 1)  # never start a real scan
    monkeypatch.setattr(security, "UNSAFE_METHODS", set())  # writes no longer need the header or a local Origin
    result = api_audit.audit(client)
    assert any("without X-Forge-Request" in f for f in result.failures)
    assert any("foreign Origin" in f for f in result.failures)


def test_the_audit_never_starts_a_scan_or_a_paid_call(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.adapter import llm

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the audit must not start a scan or call Claude")

    monkeypatch.setattr(forge, "start_find", forbidden)
    monkeypatch.setattr(llm, "get_client", forbidden)
    monkeypatch.setattr(forge, "make_client", forbidden)
    result = api_audit.audit(client)
    assert result.failures == []
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 0
    assert json.dumps(result.covered, default=list)
