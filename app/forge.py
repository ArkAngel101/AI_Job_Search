"""Forge: the orchestrator. Runs the Find pipeline on a button click and tracks its progress.

Deterministic Python only. The Find run never calls an LLM.
"""
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from app import db
from app.profile import Profile, load_profile
from app.scout.boards import load_boards
from app.scout.dedup import collapse
from app.scout.http import ScoutClient
from app.scout.location import is_us_listing
from app.scout.models import RawListing
from app.scout.scoring import score_listing
from app.scout.sources import SOURCES, fetch_all

_lock = threading.Lock()
_state: dict[str, Any] = {"active": False, "run_id": None, "sources": {}}
_thread: threading.Thread | None = None


class RunActive(Exception):
    """A Find run is already in progress."""


def make_client() -> ScoutClient:
    """The HTTP client for a scan (tests replace this with a mocked transport)."""
    return ScoutClient()


# --- starting and tracking a run ----------------------------------------------

def start_find(trigger: str = "button", background: bool = True) -> int:
    """Start a Find run and return its id. Raises RunActive if one is already running."""
    global _thread
    with _lock:
        if _state["active"]:
            raise RunActive("A run is already active")
        with db.connect() as conn:
            run_id = db.create_run(conn, trigger)
        _state.update(active=True, run_id=run_id, sources={})
    if background:
        _thread = threading.Thread(target=_run_guarded, args=(run_id,), name=f"find-{run_id}", daemon=True)
        _thread.start()
    else:
        _run_guarded(run_id)
    return run_id


def wait_for_run(timeout: float | None = None) -> None:
    """Block until the background run (if any) finishes. Used by tests."""
    if _thread is not None:
        _thread.join(timeout)


def _run_guarded(run_id: int) -> None:
    try:
        run_find(run_id)
    finally:
        with _lock:
            _state["active"] = False


def _set_progress(source: str, state: str) -> None:
    with _lock:
        _state["sources"][source] = state


def get_status() -> dict[str, Any]:
    """The current run, or the last finished one, with per-source progress."""
    with _lock:
        active, sources = bool(_state["active"]), dict(_state["sources"])
    with db.connect() as conn:
        run = db.latest_run(conn)
    if not active and run:
        sources = run["summary"].get("sources", {})
    return {"active": active, "run": run, "sources": sources}


# --- the Find pipeline -----------------------------------------------------------

def run_find(run_id: int) -> dict[str, Any]:
    """Fetch, de-duplicate, score, and remember jobs. Records the run and logs activity."""
    db.log_activity("forge", f"Find run #{run_id} started", "completed")
    try:
        summary = _scan(run_id)
    except Exception as exc:  # noqa: BLE001 - any failure must close the run record
        summary = {"error": type(exc).__name__}
        with db.connect() as conn:
            db.finish_run(conn, run_id, "failed", summary)
        db.log_activity("scout", f"Scan failed: {type(exc).__name__}", "failed")
        db.log_activity("forge", f"Find run #{run_id} failed", "failed")
        return summary
    with db.connect() as conn:
        db.finish_run(conn, run_id, "completed", summary)
    db.log_activity(
        "scout",
        f"Scanned {summary['sources_run']} sources: {summary['found']} found, "
        f"{summary['matched']} matched, {summary['new']} new",
        "completed",
    )
    db.log_activity("forge", f"Find run #{run_id} finished", "completed")
    return summary


def _scan(run_id: int) -> dict[str, Any]:
    profile = load_profile()
    names, skipped = _sources_due(profile)
    for name in profile.enabled_sources:
        _set_progress(name, "pending" if name in names else "skipped")
    notes: list[str] = list(skipped)

    try:
        boards = load_boards()
    except (ValueError, OSError) as exc:
        boards = {}
        notes.append(f"boards.yaml could not be read ({type(exc).__name__}); company boards skipped")

    with make_client() as client:
        listings, fetch_notes = fetch_all(names, profile, boards, client, on_progress=_set_progress)
    notes.extend(fetch_notes)
    _record_fetch_times(names)

    merged_all = collapse(listings)
    eligible = [m for m in merged_all if _eligible(m.listing, profile)]
    scored = [(m, score_listing(m.listing, profile, "first_cut")) for m in eligible]
    passing = sorted((p for p in scored if p[1]["score"] >= profile.first_cut), key=lambda p: -p[1]["score"])
    kept = passing[: profile.results_per_scan]

    new = 0
    with db.connect() as conn:
        for merged, result in kept:
            _, is_new = db.upsert_seen(conn, merged, result["score"], result["breakdown"], run_id)
            new += is_new
        # Everything the boards still list counts as seen, even if filtered out above.
        seen_keys = {db.find_job_key(conn, m.aliases) or m.key for m in merged_all}
        closed, reopened = db.sync_closed(conn, set(fetch_notes.completed_boards), seen_keys)

    counts: dict[str, int] = {}
    for item in listings:
        counts[item.source] = counts.get(item.source, 0) + 1
    with _lock:
        source_states = dict(_state["sources"])
    return {
        "found": len(listings),
        "unique": len(merged_all),
        "eligible": len(eligible),
        "matched": len(passing),
        "stored": len(kept),
        "new": new,
        "closed": closed,
        "reopened": reopened,
        "sources_run": len(names),
        "sources": source_states,
        "counts": counts,
        "notes": notes,
    }


def _eligible(listing: RawListing, profile: Profile) -> bool:
    """US (or unknown) location, a title, and not older than the max posting age."""
    if not listing.title or not is_us_listing(listing.location):
        return False
    created = _parse_date(listing.created)
    return created is None or datetime.now(UTC) - created <= timedelta(days=profile.max_age_days)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --- per-source scan frequency limits ------------------------------------------------

def _sources_due(profile: Profile) -> tuple[list[str], list[str]]:
    """Enabled sources to fetch now, and notes for those skipped to respect their usage terms."""
    with db.connect() as conn:
        last = db.get_setting(conn, "source_last_fetch", {})
    now = datetime.now(UTC)
    due, notes = [], []
    for name in profile.enabled_sources:
        gap = getattr(SOURCES.get(name), "MIN_SCAN_GAP_SECONDS", 0)
        previous = _parse_date(last.get(name))
        if gap and previous and (now - previous).total_seconds() < gap:
            minutes = int((gap - (now - previous).total_seconds()) // 60) + 1
            notes.append(f"{name}: skipped, fetched recently (its terms limit how often; try again in ~{minutes} min)")
        else:
            due.append(name)
    return due, notes


def _record_fetch_times(names: list[str]) -> None:
    limited = [n for n in names if getattr(SOURCES.get(n), "MIN_SCAN_GAP_SECONDS", 0)]
    if not limited:
        return
    with db.connect() as conn:
        last = db.get_setting(conn, "source_last_fetch", {})
        last.update({name: db.now_iso() for name in limited})
        db.set_setting(conn, "source_last_fetch", last)



def is_active() -> bool:
    """True while a Find run is in progress."""
    with _lock:
        return bool(_state["active"])
