"""Phase 1: schema creation, forward-only job status, and the activity log."""
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app

EXPECTED_TABLES = {
    "jobs", "runs", "activity_log", "llm_usage", "cv_versions", "tailored_cvs", "settings",
}


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app at an empty temporary data directory with a fresh schema."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    db.init_db()
    return config.DATA_DIR


def _insert_job(key: str = "j1", status: str = "seen") -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO jobs (key, title, status) VALUES (?, 'ML Engineer', ?)", (key, status))


def _job_status(key: str = "j1") -> str:
    with db.connect() as conn:
        return conn.execute("SELECT status FROM jobs WHERE key = ?", (key,)).fetchone()["status"]


# --- schema -----------------------------------------------------------------

def test_schema_creates_all_tables_and_indexes(data_dir: Path) -> None:
    assert (data_dir / "forge.db").exists()
    with db.connect() as conn:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert EXPECTED_TABLES <= tables
    assert {"idx_activity_agent", "idx_activity_status", "idx_activity_created"} <= indexes


def test_init_db_is_idempotent_and_keeps_data(data_dir: Path) -> None:
    _insert_job("keep-me")
    db.init_db()
    assert _job_status("keep-me") == "seen"


def test_jobs_columns_match_spec(data_dir: Path) -> None:
    with db.connect() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)")]
    assert cols == [
        "key", "title", "company", "location", "url", "canonical_url", "source", "source_id",
        "description", "score", "score_breakdown_json", "status", "user_status", "times_seen",
        "first_run", "last_run", "closed", "jd_json", "cv_path",
    ]


def test_new_job_defaults(data_dir: Path) -> None:
    _insert_job()
    with db.connect() as conn:
        row = conn.execute("SELECT status, user_status, times_seen, closed FROM jobs").fetchone()
    assert tuple(row) == ("seen", "new", 1, 0)


def test_invalid_user_status_rejected(data_dir: Path) -> None:
    _insert_job()
    with pytest.raises(sqlite3.IntegrityError), db.connect() as conn:
        conn.execute("UPDATE jobs SET user_status = 'hired' WHERE key = 'j1'")


def test_lifespan_creates_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "fresh")
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/health").status_code == 200
    assert (tmp_path / "fresh" / "forge.db").exists()


# --- status never downgrades ------------------------------------------------

def test_advance_status_moves_forward(data_dir: Path) -> None:
    _insert_job()
    with db.connect() as conn:
        assert db.advance_status(conn, "j1", "matched") == "matched"
        assert db.advance_status(conn, "j1", "tailored") == "tailored"
    assert _job_status() == "tailored"


@pytest.mark.parametrize(
    ("start", "attempt"),
    [("tailored", "seen"), ("tailored", "read"), ("read", "matched"), ("matched", "seen"), ("read", "read")],
)
def test_advance_status_never_downgrades(data_dir: Path, start: str, attempt: str) -> None:
    _insert_job(status=start)
    with db.connect() as conn:
        assert db.advance_status(conn, "j1", attempt) == start
    assert _job_status() == start


def test_raw_sql_downgrade_is_reverted(data_dir: Path) -> None:
    _insert_job(status="read")
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'seen', title = 'Renamed' WHERE key = 'j1'")
    with db.connect() as conn:
        row = conn.execute("SELECT status, title FROM jobs WHERE key = 'j1'").fetchone()
    assert tuple(row) == ("read", "Renamed")  # status held, other columns still updated


def test_advance_status_edge_cases(data_dir: Path) -> None:
    with db.connect() as conn:
        assert db.advance_status(conn, "missing", "read") is None
        with pytest.raises(ValueError):
            db.advance_status(conn, "missing", "bogus")


# --- activity log -----------------------------------------------------------

def test_log_activity_populates_all_fields(data_dir: Path) -> None:
    entry_id = db.log_activity("scout", "Scanned 3 sources", "completed")
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM activity_log WHERE id = ?", (entry_id,)).fetchone()
    assert row["id"] == entry_id and len(entry_id) == 32
    assert row["agent_name"] == "scout"
    assert row["task_description"] == "Scanned 3 sources"
    assert row["status"] == "completed"
    assert row["model_used"] == "none"
    assert row["created_at"]


def test_log_activity_records_model_and_failure(data_dir: Path) -> None:
    entry_id = db.log_activity("cv-adapter", "Tailor CV failed", "failed", "claude-sonnet-5-5")
    with db.connect() as conn:
        row = conn.execute("SELECT status, model_used FROM activity_log WHERE id = ?", (entry_id,)).fetchone()
    assert tuple(row) == ("failed", "claude-sonnet-5-5")


def test_log_activity_truncates_long_descriptions(data_dir: Path) -> None:
    entry_id = db.log_activity("forge", "x" * 500, "completed")
    with db.connect() as conn:
        text = conn.execute("SELECT task_description FROM activity_log WHERE id = ?", (entry_id,)).fetchone()[0]
    assert len(text) < 140 and text.endswith("…")


@pytest.mark.parametrize(("agent", "status"), [("hermes", "completed"), ("scout", "done")])
def test_log_activity_rejects_unknown_values(data_dir: Path, agent: str, status: str) -> None:
    with pytest.raises(ValueError):
        db.log_activity(agent, "nope", status)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0] == 0


def test_activity_ids_are_unique_and_ordered_by_time(data_dir: Path) -> None:
    ids = [db.log_activity("forge", f"step {i}", "completed") for i in range(3)]
    assert len(set(ids)) == 3
    with db.connect() as conn:
        newest_first = [r["id"] for r in conn.execute("SELECT id FROM activity_log ORDER BY created_at DESC")]
    assert newest_first == ids[::-1]
