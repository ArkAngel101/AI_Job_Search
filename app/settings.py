"""Settings: read the effective settings, validate and save changes, edit boards.yaml, check a board.

Only the keys the owner actually changes are stored under the `profile` setting, so everything else keeps
following the defaults (and CV suggestions). Numbers are clamped; invalid choices raise SettingsError (400).
No secret is ever included in what this module returns.
"""
import os
import platform
import re
from collections.abc import Callable
from typing import Any

import yaml

from app import config, db
from app.adapter import llm
from app.profile import SOURCE_NAMES, Profile, load_profile
from app.scout.boards import ATS_NAMES, SLUG_RE, Board, boards_path, ensure_boards_file, parse_boards
from app.scout.http import FetchError, ScoutClient
from app.scout.sources import SOURCES, ashby, greenhouse, lever, workable

SENIORITY = ("entry", "mid", "senior", "lead", "principal", "manager", "any")
LISTS = {  # name: (max items, max words per item, max chars per item, may be empty)
    "titles": (30, 6, 60, False), "keywords": (30, 2, 40, False), "scoring_skills": (100, 4, 40, True),
}
INT_RANGES = {"first_cut": (0, 100), "final": (0, 100), "pages": (1, 5), "max_age_days": (1, 365), "results_per_scan": (10, 300)}
PROFILE_KEYS = ("seniority", *LISTS, *INT_RANGES, "enabled_sources")
MAX_BOARDS_YAML_CHARS = 50_000
MAX_PRICE = 10_000.0
MAX_CAP = 100_000.0

SOURCE_LABELS = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workable": "Workable", "adzuna": "Adzuna",
    "remotive": "Remotive", "remoteok": "RemoteOK", "themuse": "The Muse", "jobicy": "Jobicy",
}
SOURCE_KINDS = {"greenhouse": "company boards", "lever": "company boards", "ashby": "company boards",
                "workable": "company boards", "adzuna": "US job search", "remotive": "remote jobs",
                "remoteok": "remote jobs", "themuse": "US jobs", "jobicy": "remote jobs"}


class SettingsError(ValueError):
    """The requested change is invalid (HTTP 400). The message is safe to show."""


# --- reading ---------------------------------------------------------------------------------

def _source_info(enabled: list[str]) -> list[dict[str, Any]]:
    info = []
    for name in SOURCE_NAMES:
        env = list(getattr(SOURCES[name], "REQUIRES_ENV", ()))
        gap = getattr(SOURCES[name], "MIN_SCAN_GAP_SECONDS", 0)
        info.append({
            "name": name, "label": SOURCE_LABELS[name], "kind": SOURCE_KINDS[name], "enabled": name in enabled,
            "needs_key": bool(env), "key_set": all(os.environ.get(v, "").strip() for v in env) if env else None,
            "key_names": env, "min_hours_between_fetches": round(gap / 3600, 1) if gap else None,
        })
    return info


def effective_settings() -> dict[str, Any]:
    """Everything the Settings page shows. Secrets appear only as set / not set."""
    profile = load_profile()
    with db.connect() as conn:
        stored = db.get_setting(conn, "profile", {})
        prices = db.get_setting(conn, "claude_prices")
        cap = db.get_setting(conn, "monthly_cap_usd")
        spent = db.month_spend(conn)
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("jobs", "runs", "activity_log")}
    db_file = db.db_path()
    return {
        "profile": {key: getattr(profile, key) for key in PROFILE_KEYS},
        "customized": sorted(set(stored) & set(PROFILE_KEYS)),
        "defaults": {key: getattr(Profile(), key) for key in PROFILE_KEYS},
        "sources": _source_info(profile.enabled_sources),
        "boards_yaml": ensure_boards_file().read_text(encoding="utf-8"),
        "claude": {
            "model": llm.model_name(), "model_from_env": bool(os.environ.get("CLAUDE_MODEL", "").strip()),
            "key_set": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()), "prices": prices,
            "monthly_cap_usd": cap or None, "spent_this_month_usd": round(spent, 4),
        },
        "system": {
            "data_dir": str(config.DATA_DIR), "database_bytes": db_file.stat().st_size if db_file.exists() else 0,
            "jobs": counts["jobs"], "runs": counts["runs"], "activity_entries": counts["activity_log"],
            "python": platform.python_version(), "listening_on": "127.0.0.1:8000 (this computer only)",
            "scheduler": "none: every scan and AI call starts from a button click",
        },
    }


# --- validation ---------------------------------------------------------------------------------

def _clamp(name: str, value: Any) -> int:
    lo, hi = INT_RANGES[name]
    if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
        raise SettingsError(f"{name} must be a number")
    return max(lo, min(hi, int(round(value))))


def _clean_list(name: str, value: Any) -> list[str]:
    max_items, max_words, max_chars, may_be_empty = LISTS[name]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SettingsError(f"{name} must be a list of text items")
    items: list[str] = []
    for raw in value:
        item = " ".join("".join(ch for ch in raw if ch.isprintable()).split())
        if not item:
            continue
        if len(item) > max_chars or len(item.split()) > max_words:
            raise SettingsError(f"'{item[:30]}' is too long for {name} (at most {max_words} words, {max_chars} characters)")
        if item.lower() not in {i.lower() for i in items}:
            items.append(item)
    if len(items) > max_items:
        raise SettingsError(f"{name} can hold at most {max_items} items")
    if not items and not may_be_empty:
        raise SettingsError(f"{name} cannot be empty")
    return items


def _clean_profile(changes: Any) -> dict[str, Any]:
    if not isinstance(changes, dict):
        raise SettingsError("profile must be an object")
    unknown = set(changes) - set(PROFILE_KEYS)
    if unknown:
        raise SettingsError(f"Unknown setting: {sorted(unknown)[0]}")
    clean: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "seniority":
            if value not in SENIORITY:
                raise SettingsError(f"seniority must be one of: {', '.join(SENIORITY)}")
            clean[key] = value
        elif key in LISTS:
            clean[key] = _clean_list(key, value)
        elif key in INT_RANGES:
            clean[key] = _clamp(key, value)
        elif key == "enabled_sources":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise SettingsError("enabled_sources must be a list of source names")
            if set(value) - set(SOURCE_NAMES):
                raise SettingsError(f"Unknown source: {sorted(set(value) - set(SOURCE_NAMES))[0]}")
            if not value:
                raise SettingsError("At least one source must stay on")
            clean[key] = [name for name in SOURCE_NAMES if name in value]
    return clean


def _clean_number(name: str, value: Any, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value != value or value < 0 or value > maximum:
        raise SettingsError(f"{name} must be a number between 0 and {maximum:g}")
    return float(value)


def apply_update(body: dict[str, Any]) -> None:
    """Validate and save a partial update: {"profile": {...}, "claude": {...}, "reset": [...]}."""
    unknown = set(body) - {"profile", "claude", "reset"}
    if unknown:
        raise SettingsError(f"Unknown field: {sorted(unknown)[0]}")
    changes = _clean_profile(body.get("profile", {}))
    reset = body.get("reset", [])
    if not isinstance(reset, list) or set(map(str, reset)) - set(PROFILE_KEYS):
        raise SettingsError("reset must list setting names")

    claude = body.get("claude", {})
    if not isinstance(claude, dict) or set(claude) - {"prices", "monthly_cap_usd"}:
        raise SettingsError("claude accepts only prices and monthly_cap_usd")
    prices: Any = "keep"
    if "prices" in claude:
        raw = claude["prices"]
        if raw is None:
            prices = None
        elif isinstance(raw, dict) and set(raw) == {"input_per_mtok", "output_per_mtok"}:
            prices = {k: _clean_number(k, v, MAX_PRICE) for k, v in raw.items()}
        else:
            raise SettingsError("prices needs input_per_mtok and output_per_mtok (dollars per million tokens)")
    cap: Any = "keep"
    if "monthly_cap_usd" in claude:
        cap = None if claude["monthly_cap_usd"] is None else _clean_number("monthly_cap_usd", claude["monthly_cap_usd"], MAX_CAP)

    # thresholds: the final threshold can never sit below the first cut
    if "first_cut" in changes or "final" in changes:
        current = load_profile()
        first, final = changes.get("first_cut", current.first_cut), changes.get("final", current.final)
        if final < first:
            changes["final"] = first
        changes.setdefault("first_cut", first)
        changes.setdefault("final", final if final >= first else first)

    with db.connect() as conn:  # one transaction: all of it is saved, or none of it
        stored = db.get_setting(conn, "profile", {})
        stored.update(changes)
        for key in reset:
            stored.pop(str(key), None)
        if stored:
            db.set_setting(conn, "profile", stored)
        else:
            db.delete_setting(conn, "profile")
        if prices != "keep":
            (db.delete_setting(conn, "claude_prices") if prices is None else db.set_setting(conn, "claude_prices", prices))
        if cap != "keep":
            (db.delete_setting(conn, "monthly_cap_usd") if not cap else db.set_setting(conn, "monthly_cap_usd", cap))


# --- boards.yaml ----------------------------------------------------------------------------------

def save_boards_yaml(text: Any) -> dict[str, int]:
    """Validate and save the boards file (atomic). Returns the number of boards per ATS."""
    if not isinstance(text, str) or len(text) > MAX_BOARDS_YAML_CHARS:
        raise SettingsError(f"boards must be text of at most {MAX_BOARDS_YAML_CHARS:,} characters")
    try:
        boards = parse_boards(text)
    except (ValueError, yaml.YAMLError) as exc:
        raise SettingsError(f"boards.yaml is not valid: {_first_line(exc)}") from None
    path = ensure_boards_file()
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    os.replace(temp, boards_path())
    return {ats: len(items) for ats, items in boards.items()}


def _first_line(exc: Exception) -> str:
    return re.sub(r"\s+", " ", str(exc).splitlines()[0] if str(exc) else type(exc).__name__)[:200]


_FETCHERS: dict[str, Callable[[Board, ScoutClient], list[Any]]] = {
    "greenhouse": greenhouse.fetch_board, "lever": lever.fetch_board, "ashby": ashby.fetch_board, "workable": workable.fetch_board,
}


def make_client() -> ScoutClient:
    """HTTP client for the slug check (tests replace this)."""
    return ScoutClient()


def check_board(ats: str, slug: str) -> dict[str, Any]:
    """Ask the ATS's public API whether a board slug exists and how many jobs it lists."""
    if ats not in ATS_NAMES or not SLUG_RE.match(slug):
        raise SettingsError("Unknown ATS or invalid slug")
    with make_client() as client:
        try:
            jobs = _FETCHERS[ats](Board(slug=slug, name=slug), client)
        except FetchError as exc:
            if exc.status == 404:
                return {"ok": False, "detail": f"No {SOURCE_LABELS[ats]} board named '{slug}' was found (404)."}
            return {"ok": False, "detail": f"Could not check the board ({exc})."}
        except (KeyError, TypeError, AttributeError):
            return {"ok": False, "detail": "The board answered in an unexpected format."}
    return {"ok": True, "jobs": len(jobs), "detail": f"Found {len(jobs)} open job{'s' if len(jobs) != 1 else ''}."}

