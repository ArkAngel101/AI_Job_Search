"""SQLite schema, connections, and write helpers. All database writes go through this module."""
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app import config

# Job status only moves forward through this order.
JOB_STATUSES = ("seen", "matched", "read", "tailored")
USER_STATUSES = ("new", "interested", "applied", "interview", "rejected")
AGENTS = ("forge", "scout", "job-reader", "cv-adapter")
ACTIVITY_STATUSES = ("completed", "failed")
MAX_DESCRIPTION_LEN = 139  # activity descriptions stay under 140 characters


def _in_list(values: tuple[str, ...]) -> str:
    """SQL list literal for CHECK constraints (built from module constants only)."""
    return ", ".join(f"'{v}'" for v in values)


def _status_rank_sql(expr: str) -> str:
    """SQL CASE expression turning a job status into its forward-order rank."""
    whens = " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(JOB_STATUSES))
    return f"(CASE {expr} {whens} END)"


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
    key TEXT PRIMARY KEY,
    title TEXT,
    company TEXT,
    location TEXT,
    url TEXT,
    canonical_url TEXT,
    source TEXT,
    source_id TEXT,
    description TEXT,
    score REAL,
    score_breakdown_json TEXT,
    status TEXT NOT NULL DEFAULT 'seen' CHECK (status IN ({_in_list(JOB_STATUSES)})),
    user_status TEXT NOT NULL DEFAULT 'new' CHECK (user_status IN ({_in_list(USER_STATUSES)})),
    times_seen INTEGER NOT NULL DEFAULT 1,
    first_run INTEGER,
    last_run INTEGER,
    closed INTEGER NOT NULL DEFAULT 0,
    jd_json TEXT,
    cv_path TEXT,
    board TEXT
);

-- Every identity a job has been seen under (id, canonical url, fuzzy key), so later
-- sightings from other sources still match the same row.
CREATE TABLE IF NOT EXISTS job_aliases (
    alias_type TEXT NOT NULL CHECK (alias_type IN ('id', 'url', 'fuzzy')),
    alias_value TEXT NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (alias_type, alias_value)
);
CREATE INDEX IF NOT EXISTS idx_job_aliases_key ON job_aliases (key);

-- Backstop: any write path that tries to move status backwards is reverted.
CREATE TRIGGER IF NOT EXISTS jobs_status_forward_only
AFTER UPDATE OF status ON jobs
WHEN {_status_rank_sql("NEW.status")} < {_status_rank_sql("OLD.status")}
BEGIN
    UPDATE jobs SET status = OLD.status WHERE key = NEW.key;
END;

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT,
    status TEXT,
    started_at TEXT,
    ended_at TEXT,
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS activity_log (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL CHECK (agent_name IN ({_in_list(AGENTS)})),
    task_description TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ({_in_list(ACTIVITY_STATUSES)})),
    model_used TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_agent ON activity_log (agent_name);
CREATE INDEX IF NOT EXISTS idx_activity_status ON activity_log (status);
CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log (created_at DESC);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    job_key TEXT,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    est_cost_usd REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cv_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    raw_filename TEXT,
    base_cv_json TEXT,
    valid INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tailored_cvs (
    job_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    cv_json TEXT NOT NULL,
    docx_path TEXT,
    pdf_path TEXT,
    validation_json TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
"""


def db_path() -> Path:
    """Location of the database file (inside the data directory)."""
    return config.DATA_DIR / "forge.db"


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (sorts chronologically)."""
    return datetime.now(UTC).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open a connection; commit on success, roll back on error, always close."""
    conn = sqlite3.connect(db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create the data directory, database file, and any missing tables."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring databases created by earlier phases up to date."""
    job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "board" not in job_columns:  # added in Phase 3
        conn.execute("ALTER TABLE jobs ADD COLUMN board TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_board ON jobs (board)")


def advance_status(conn: sqlite3.Connection, key: str, new_status: str) -> str | None:
    """Move a job's status forward only; return its resulting status (None if no such job)."""
    if new_status not in JOB_STATUSES:
        raise ValueError(f"Unknown job status: {new_status!r}")
    row = conn.execute("SELECT status FROM jobs WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    current = row["status"]
    if JOB_STATUSES.index(new_status) > JOB_STATUSES.index(current):
        conn.execute("UPDATE jobs SET status = ? WHERE key = ?", (new_status, key))
        return new_status
    return current


def find_job_key(conn: sqlite3.Connection, aliases: list[tuple[str, str]]) -> str | None:
    """Key of the stored job matching any alias, checking id, then url, then fuzzy (strongest first)."""
    for kind in ("id", "url", "fuzzy"):
        for alias_kind, value in aliases:
            if alias_kind != kind:
                continue
            row = conn.execute(
                "SELECT key FROM job_aliases WHERE alias_type = ? AND alias_value = ?", (kind, value)
            ).fetchone()
            if row is not None:
                return row["key"]
    return None


def add_aliases(conn: sqlite3.Connection, key: str, aliases: list[tuple[str, str]]) -> None:
    """Record aliases for a job. An alias already owned by another job keeps its owner."""
    conn.executemany(
        "INSERT OR IGNORE INTO job_aliases (alias_type, alias_value, key) VALUES (?, ?, ?)",
        [(kind, value, key) for kind, value in aliases],
    )


def sources_seen(conn: sqlite3.Connection, key: str) -> list[str]:
    """Every source name this job has been listed under."""
    rows = conn.execute(
        "SELECT alias_value FROM job_aliases WHERE key = ? AND alias_type = 'id' ORDER BY alias_value", (key,)
    )
    return sorted({row["alias_value"].split(":", 1)[0] for row in rows})


def sync_closed(conn: sqlite3.Connection, completed_boards: set[str], seen_keys: set[str]) -> tuple[int, int]:
    """Mark jobs closed or reopened after a scan; return (newly closed, reopened).

    Only boards fetched successfully and completely may close jobs, so a failed or partial
    fetch never closes anything. Any job seen again this scan is reopened.
    """
    closed = 0
    for board in completed_boards:
        gone = [
            row["key"]
            for row in conn.execute("SELECT key FROM jobs WHERE board = ? AND closed = 0", (board,))
            if row["key"] not in seen_keys
        ]
        conn.executemany("UPDATE jobs SET closed = 1 WHERE key = ?", [(key,) for key in gone])
        closed += len(gone)
    reopen = [row["key"] for row in conn.execute("SELECT key FROM jobs WHERE closed = 1") if row["key"] in seen_keys]
    conn.executemany("UPDATE jobs SET closed = 0 WHERE key = ?", [(key,) for key in reopen])
    return closed, len(reopen)


def log_activity(agent: str, description: str, status: str, model: str = "none") -> str:
    """Record one agent action in the activity log and return its id.

    Descriptions are cut to under 140 characters. `model` is "none" for non-LLM agents.
    """
    if agent not in AGENTS:
        raise ValueError(f"Unknown agent: {agent!r}")
    if status not in ACTIVITY_STATUSES:
        raise ValueError(f"Unknown activity status: {status!r}")
    text = " ".join(description.split())
    if len(text) > MAX_DESCRIPTION_LEN:
        text = text[: MAX_DESCRIPTION_LEN - 1] + "…"
    entry_id = uuid.uuid4().hex
    with connect() as conn:
        conn.execute(
            "INSERT INTO activity_log (id, agent_name, task_description, status, model_used, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (entry_id, agent, text, status, model or "none", now_iso()),
        )
    return entry_id
