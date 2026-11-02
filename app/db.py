"""SQLite schema, connections, and write helpers. All database writes go through this module."""
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import config
from app.scout.dedup import MergedListing, canonical_url, is_better, job_key
from app.scout.dedup import aliases as listing_aliases
from app.scout.models import RawListing

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


# --- settings ----------------------------------------------------------------

def scrub_secret_urls(conn: sqlite3.Connection, secrets: list[str]) -> int:
    """Remove secret values from stored job links (older scans stored Adzuna's app id in them). Returns rows fixed."""
    from app.scout.text import scrub_url

    usable = [s for s in secrets if len(s) >= 6]
    fixed = 0
    for row in conn.execute("SELECT key, url FROM jobs WHERE url IS NOT NULL").fetchall():
        if any(secret in row["url"] for secret in usable):
            conn.execute("UPDATE jobs SET url = ? WHERE key = ?", (scrub_url(row["url"], usable), row["key"]))
            fixed += 1
    return fixed


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    """Read a JSON setting."""
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row["value_json"])


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Write a JSON setting (insert or replace this key only; other keys are untouched)."""
    conn.execute(
        "INSERT INTO settings (key, value_json) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
        (key, json.dumps(value)),
    )


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    """Remove a setting (it falls back to its default)."""
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))


# --- runs --------------------------------------------------------------------

def create_run(conn: sqlite3.Connection, trigger: str) -> int:
    """Start a run record in the 'running' state and return its id."""
    cur = conn.execute(
        "INSERT INTO runs (trigger, status, started_at) VALUES (?, 'running', ?)", (trigger, now_iso())
    )
    return int(cur.lastrowid or 0)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, summary: dict[str, Any]) -> None:
    """Close a run as 'completed' or 'failed' with its summary."""
    conn.execute(
        "UPDATE runs SET status = ?, ended_at = ?, summary_json = ? WHERE id = ?",
        (status, now_iso(), json.dumps(summary), run_id),
    )


def fail_stale_runs(conn: sqlite3.Connection) -> int:
    """Mark runs left 'running' by a previous process as failed; return how many."""
    cur = conn.execute(
        "UPDATE runs SET status = 'failed', ended_at = ?, summary_json = ? WHERE status = 'running'",
        (now_iso(), json.dumps({"error": "interrupted (the app restarted during this run)"})),
    )
    return cur.rowcount


def latest_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The most recent run with its summary decoded, or None."""
    row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    run = dict(row)
    run["summary"] = json.loads(run.pop("summary_json") or "{}")
    return run


# --- job memory --------------------------------------------------------------

def upsert_seen(
    conn: sqlite3.Connection, merged: MergedListing, score: int, breakdown: dict[str, Any], run_id: int
) -> tuple[str, bool]:
    """Insert a job, or bump times_seen / last_run on one already known. Returns (key, is_new).

    A job matches an existing row on any alias (id, url, fuzzy). Status only moves forward. A better
    listing (ATS first, then fuller description) replaces the stored listing fields. Jobs already read
    keep their final-mode score.
    """
    item = merged.listing
    key = find_job_key(conn, merged.aliases) or merged.key
    row = conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone()
    breakdown_json = json.dumps(breakdown)
    if row is None:
        conn.execute(
            "INSERT INTO jobs (key, title, company, location, url, canonical_url, source, source_id, description,"
            " score, score_breakdown_json, status, times_seen, first_run, last_run, closed, board)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'matched', 1, ?, ?, 0, ?)",
            (key, item.title, item.company, item.location, item.url, canonical_url(item.url), item.source,
             item.source_id, item.description, score, breakdown_json, run_id, run_id, merged.board),
        )
        add_aliases(conn, key, merged.aliases)
        return key, True

    conn.execute("UPDATE jobs SET times_seen = times_seen + 1, last_run = ? WHERE key = ?", (run_id, key))
    if is_better(item.source, item.description, row["source"] or "", row["description"] or ""):
        conn.execute(
            "UPDATE jobs SET title = ?, company = ?, location = ?, url = ?, canonical_url = ?, source = ?,"
            " source_id = ?, description = ? WHERE key = ?",
            (item.title, item.company, item.location, item.url, canonical_url(item.url), item.source,
             item.source_id, item.description, key),
        )
    if merged.board and not row["board"]:
        conn.execute("UPDATE jobs SET board = ? WHERE key = ?", (merged.board, key))
    if row["status"] in ("seen", "matched"):  # read/tailored jobs keep their final score
        conn.execute("UPDATE jobs SET score = ?, score_breakdown_json = ? WHERE key = ?", (score, breakdown_json, key))
        advance_status(conn, key, "matched")
    add_aliases(conn, key, merged.aliases)
    return key, False


_SUMMARY_COLUMNS = (
    "key, title, company, location, url, source, score, times_seen, closed, status, user_status, first_run,"
    " COALESCE(json_extract(score_breakdown_json, '$.flags.needs_sponsorship_or_citizenship'), 0) AS needs_sponsorship"
)


def safe_url(url: str | None) -> str:
    """The URL if it is http(s), else "" (so a javascript: or data: link can never reach the UI)."""
    return url if url and url.lower().startswith(("http://", "https://")) else ""


def _summary(row: sqlite3.Row, rank: int | None, latest_run_id: int | None) -> dict[str, Any]:
    return {
        "rank": rank, "score": row["score"], "title": row["title"], "company": row["company"],
        "location": row["location"], "url": safe_url(row["url"]), "source": row["source"],
        "new": row["first_run"] is not None and row["first_run"] == latest_run_id,
        "times_seen": row["times_seen"], "closed": bool(row["closed"]), "status": row["status"],
        "user_status": row["user_status"], "key": row["key"], "needs_sponsorship": bool(row["needs_sponsorship"]),
    }


def list_jobs(
    conn: sqlite3.Connection,
    *,
    min_score: int | None = None,
    new_only: bool = False,
    hide_closed: bool = False,
    status: str | None = None,
    user_status: str | None = None,
    source: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Ranked job rows (best score first) with optional filters."""
    latest = conn.execute("SELECT MAX(id) AS id FROM runs").fetchone()["id"]
    where, args = ["1=1"], []
    if min_score is not None:
        where.append("score >= ?")
        args.append(min_score)
    if new_only:
        where.append("first_run = ?")
        args.append(latest)
    if hide_closed:
        where.append("closed = 0")
    if status:
        where.append("status = ?")
        args.append(status)
    if user_status:
        where.append("user_status = ?")
        args.append(user_status)
    if source:
        where.append("source = ?")
        args.append(source)
    if q:
        where.append("(title LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\')")
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args += [pattern, pattern]
    rows = conn.execute(
        f"SELECT {_SUMMARY_COLUMNS} FROM jobs WHERE {' AND '.join(where)}"
        " ORDER BY score DESC, last_run DESC, title ASC LIMIT ? OFFSET ?",
        (*args, limit, offset),
    ).fetchall()
    return [_summary(row, offset + index, latest) for index, row in enumerate(rows, start=1)]


def get_job_summary(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """One job in the same shape as list_jobs rows (rank is None), or None if unknown."""
    latest = conn.execute("SELECT MAX(id) AS id FROM runs").fetchone()["id"]
    row = conn.execute(f"SELECT {_SUMMARY_COLUMNS} FROM jobs WHERE key = ?", (key,)).fetchone()
    return None if row is None else _summary(row, None, latest)


def overview_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Funnel counts: found (last scan), matched, read, tailored, known total, new in last run."""
    run = latest_run(conn)
    run_id = run["id"] if run else None
    counts = conn.execute(
        "SELECT COUNT(*) AS known,"
        " SUM(status IN ('matched', 'read', 'tailored')) AS matched,"
        " SUM(status IN ('read', 'tailored')) AS read,"
        " SUM(status = 'tailored') AS tailored,"
        " SUM(first_run = ?) AS new FROM jobs",
        (run_id,),
    ).fetchone()
    return {
        "found": int((run or {}).get("summary", {}).get("found", 0)),
        "matched": int(counts["matched"] or 0),
        "read": int(counts["read"] or 0),
        "tailored": int(counts["tailored"] or 0),
        "known": int(counts["known"] or 0),
        "new": int(counts["new"] or 0),
    }


def insert_added_job(conn: sqlite3.Connection, item: RawListing, board: str | None) -> tuple[str, bool]:
    """Store a job the owner added by URL or text. Returns (key, created); a known job is reused."""
    alias_list = listing_aliases(item)
    existing = find_job_key(conn, alias_list)
    if existing:
        return existing, False
    key = job_key(item.source, item.source_id)
    conn.execute(
        "INSERT INTO jobs (key, title, company, location, url, canonical_url, source, source_id, description,"
        " status, times_seen, closed, board) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'seen', 1, 0, ?)",
        (key, item.title, item.company, item.location, item.url, canonical_url(item.url), item.source,
         item.source_id, item.description, board),
    )
    add_aliases(conn, key, alias_list)
    return key, True


def save_jd(conn: sqlite3.Connection, key: str, jd: dict[str, Any], score: int, breakdown: dict[str, Any]) -> None:
    """Cache the extracted job description, store the final-mode score, and mark the job read."""
    conn.execute(
        "UPDATE jobs SET jd_json = ?, score = ?, score_breakdown_json = ? WHERE key = ?",
        (json.dumps(jd), score, json.dumps(breakdown), key),
    )
    advance_status(conn, key, "read")


# --- LLM usage and CV versions -------------------------------------------------

def record_llm_usage(
    conn: sqlite3.Connection, job_key: str | None, model: str, input_tokens: int, output_tokens: int, cost_usd: float
) -> None:
    """Log one AI call's token usage and estimated cost."""
    conn.execute(
        "INSERT INTO llm_usage (ts, job_key, model, input_tokens, output_tokens, est_cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
        (now_iso(), job_key, model, input_tokens, output_tokens, cost_usd),
    )


def month_spend(conn: sqlite3.Connection) -> float:
    """Estimated AI spend so far this calendar month (UTC)."""
    start = datetime.now(UTC).strftime("%Y-%m-01")
    row = conn.execute("SELECT COALESCE(SUM(est_cost_usd), 0) AS total FROM llm_usage WHERE ts >= ?", (start,)).fetchone()
    return float(row["total"])


def insert_cv_version(conn: sqlite3.Connection, raw_filename: str, base_cv_json: str | None, valid: bool) -> int:
    """Record a CV build attempt (valid=0 rows keep a failed build visible without replacing the base CV)."""
    cur = conn.execute(
        "INSERT INTO cv_versions (created_at, raw_filename, base_cv_json, valid) VALUES (?, ?, ?, ?)",
        (now_iso(), raw_filename, base_cv_json, int(valid)),
    )
    return int(cur.lastrowid or 0)


# --- tailored CVs ------------------------------------------------------------------

def get_tailored(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """The tailored CV stored for a job (JSON fields decoded), or None."""
    row = conn.execute("SELECT * FROM tailored_cvs WHERE job_key = ?", (key,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["cv"] = json.loads(data.pop("cv_json"))
    data["validation"] = json.loads(data.pop("validation_json") or "{}")
    return data


def save_tailored(
    conn: sqlite3.Connection, key: str, cv: dict[str, Any], docx_path: str | None, pdf_path: str | None,
    validation: dict[str, Any],
) -> None:
    """Store (or replace) a job's tailored CV, point the job at its .docx, and mark the job tailored."""
    conn.execute(
        "INSERT INTO tailored_cvs (job_key, created_at, cv_json, docx_path, pdf_path, validation_json)"
        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(job_key) DO UPDATE SET created_at = excluded.created_at,"
        " cv_json = excluded.cv_json, docx_path = excluded.docx_path, pdf_path = excluded.pdf_path,"
        " validation_json = excluded.validation_json",
        (key, now_iso(), json.dumps(cv), docx_path, pdf_path, json.dumps(validation)),
    )
    conn.execute("UPDATE jobs SET cv_path = ? WHERE key = ?", (docx_path, key))
    advance_status(conn, key, "tailored")


def set_export_paths(conn: sqlite3.Connection, key: str, docx_path: str | None, pdf_path: str | None) -> None:
    """Update where a tailored CV's files live (after they are regenerated)."""
    conn.execute("UPDATE tailored_cvs SET docx_path = ?, pdf_path = ? WHERE job_key = ?", (docx_path, pdf_path, key))
    conn.execute("UPDATE jobs SET cv_path = ? WHERE key = ?", (docx_path, key))


# --- UI support: agents, activity, owner's job status -----------------------------------

def agent_stats(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Per agent: task count, completed count, and the latest action with its model and time."""
    stats: dict[str, dict[str, Any]] = {}
    for agent in AGENTS:
        row = conn.execute(
            "SELECT COUNT(*) AS tasks, COALESCE(SUM(status = 'completed'), 0) AS completed FROM activity_log WHERE agent_name = ?",
            (agent,),
        ).fetchone()
        last = conn.execute(
            "SELECT task_description, model_used, status, created_at FROM activity_log WHERE agent_name = ?"
            " ORDER BY created_at DESC LIMIT 1", (agent,),
        ).fetchone()
        stats[agent] = {
            "tasks": int(row["tasks"]), "completed": int(row["completed"]),
            "last_action": last["task_description"] if last else None,
            "last_model": last["model_used"] if last else None,
            "last_status": last["status"] if last else None,
            "last_at": last["created_at"] if last else None,
        }
    return stats


def list_activity(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """The newest activity entries first."""
    rows = conn.execute(
        "SELECT id, agent_name, task_description, status, model_used, created_at FROM activity_log"
        " ORDER BY created_at DESC LIMIT ?", (limit,),
    ).fetchall()
    return [
        {"id": r["id"], "agent": r["agent_name"], "task": r["task_description"], "status": r["status"],
         "model": r["model_used"], "at": r["created_at"]}
        for r in rows
    ]


def set_user_status(conn: sqlite3.Connection, key: str, user_status: str) -> bool:
    """Save the owner's own status for a job (new, interested, applied, interview, rejected)."""
    if user_status not in USER_STATUSES:
        raise ValueError(f"Unknown status: {user_status!r}")
    return conn.execute("UPDATE jobs SET user_status = ? WHERE key = ?", (user_status, key)).rowcount > 0


def get_job_detail(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """What the JD drawer shows: the listing, the extracted description (if read), and the score breakdown."""
    row = conn.execute(
        "SELECT key, title, company, location, url, source, description, jd_json, score_breakdown_json, score, status FROM jobs WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    return {
        "key": row["key"], "title": row["title"], "company": row["company"], "location": row["location"],
        "url": safe_url(row["url"]), "source": row["source"], "score": row["score"], "status": row["status"],
        "read": row["jd_json"] is not None, "jd": json.loads(row["jd_json"]) if row["jd_json"] else None,
        "description": row["description"] or "",
        "score_breakdown": json.loads(row["score_breakdown_json"]) if row["score_breakdown_json"] else None,
    }
