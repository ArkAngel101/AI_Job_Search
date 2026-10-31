"""Job source registry and the fault-isolating runner."""
from collections.abc import Callable
from types import ModuleType

from app.profile import Profile
from app.scout.boards import Board
from app.scout.http import FetchError, ScoutClient
from app.scout.models import Notes, RawListing
from app.scout.sources import adzuna, ashby, greenhouse, jobicy, lever, remoteok, remotive, themuse, workable

SOURCES: dict[str, ModuleType] = {
    module.NAME: module
    for module in (greenhouse, lever, ashby, workable, adzuna, remotive, remoteok, themuse, jobicy)
}

ProgressCallback = Callable[[str, str], None]  # (source name, "running" | "done" | "failed")


def fetch_all(
    names: list[str],
    profile: Profile,
    boards: dict[str, list[Board]],
    client: ScoutClient,
    on_progress: ProgressCallback | None = None,
) -> tuple[list[RawListing], Notes]:
    """Fetch every named source. One source failing is noted and the rest still run."""
    listings: list[RawListing] = []
    notes = Notes()
    for name in names:
        module = SOURCES.get(name)
        if module is None:
            notes.append(f"{name}: unknown source, skipped")
            continue
        if on_progress:
            on_progress(name, "running")
        try:
            found, source_notes = module.fetch(profile, boards, client)
        except Exception as exc:  # noqa: BLE001 - a failing source must never abort the run
            # Only FetchError messages are known to be free of URLs and secrets.
            detail = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
            notes.append(f"{name}: failed ({detail})")
            if on_progress:
                on_progress(name, "failed")
            continue
        listings.extend(found)
        notes.extend(source_notes)
        notes.completed_boards |= getattr(source_notes, "completed_boards", set())
        if on_progress:
            on_progress(name, "done")
    return listings, notes
