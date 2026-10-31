"""Shared loop for company-board (ATS) sources."""
from collections.abc import Callable

from app.scout.boards import Board
from app.scout.http import FetchError, ScoutClient
from app.scout.models import Notes, RawListing


def run_boards(
    source: str,
    boards: list[Board],
    client: ScoutClient,
    fetch_board: Callable[[Board, ScoutClient], list[RawListing]],
) -> tuple[list[RawListing], Notes]:
    """Fetch each board. A bad slug (404) or failure is noted and skipped, never fatal.

    Boards fetched completely are recorded in `notes.completed_boards` (used to mark closed jobs).
    """
    listings: list[RawListing] = []
    notes = Notes()
    for board in boards:
        label = f"{source}/{board.slug}"
        try:
            found = fetch_board(board, client)
        except FetchError as exc:
            if exc.status == 404:
                notes.append(f"{label}: board not found (404), skipped. Check the slug in boards.yaml")
            else:
                notes.append(f"{label}: fetch failed ({exc})")
            continue
        except (KeyError, TypeError, AttributeError):
            notes.append(f"{label}: unexpected response shape, skipped")
            continue
        for item in found:
            item.board = label
        listings.extend(found)
        notes.completed_boards.add(label)
    return listings, notes
