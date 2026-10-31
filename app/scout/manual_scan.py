"""Manual live check of all enabled sources. Not part of the tests; run it yourself.

    podman exec forge python -m app.scout.manual_scan
"""
from collections import Counter

from app.profile import Profile
from app.scout.boards import load_boards
from app.scout.http import ScoutClient
from app.scout.sources import fetch_all


def main() -> None:
    """Fetch every enabled source with the default profile and print a per-source summary."""
    profile = Profile()
    with ScoutClient() as client:
        listings, notes = fetch_all(
            profile.enabled_sources, profile, load_boards(), client,
            on_progress=lambda name, state: print(f"{name}: {state}", flush=True),
        )
    print("\nListings per source:")
    for source, count in sorted(Counter(item.source for item in listings).items()):
        print(f"  {source}: {count}")
    print("\nNotes:")
    for note in notes or ["(none)"]:
        print(f"  {note}")


if __name__ == "__main__":
    main()
