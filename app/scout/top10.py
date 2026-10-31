"""Print the top 10 of a scored fixture set with breakdowns.

    python -m app.scout.top10 tests/fixtures/scoring_set.json [first_cut|final]
"""
import json
import sys
from pathlib import Path

from app.profile import Profile
from app.scout.location import is_us_listing
from app.scout.models import RawListing
from app.scout.scoring import score_listing

_FIELDS = RawListing.__slots__


def load_listings(path: Path) -> list[RawListing]:
    """Read listings from a JSON list, ignoring extra keys such as test labels."""
    return [RawListing(**{k: v for k, v in item.items() if k in _FIELDS}) for item in json.loads(path.read_text())]


def top(listings: list[RawListing], profile: Profile, mode: str, n: int = 10) -> list[tuple[RawListing, dict]]:
    """Drop non-US listings, score the rest, and return the best n (highest score first)."""
    scored = [(item, score_listing(item, profile, mode)) for item in listings if is_us_listing(item.location)]
    return sorted(scored, key=lambda pair: -pair[1]["score"])[:n]


def main() -> None:
    """CLI entry point."""
    path = Path(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "first_cut"
    for rank, (item, result) in enumerate(top(load_listings(path), Profile(), mode), start=1):
        b = result["breakdown"]
        print(
            f"{rank:>2}. {result['score']:>3}  {item.title} | {item.company} | {item.location}\n"
            f"       title {b['title']['points']}  skill {b['skill']['points']} ({len(b['skill']['hits'])}/{b['skill']['total']})  "
            f"seniority {b['seniority']['points']} [{b['seniority']['detected']}]  location {b['location']['points']}  "
            f"flags {[k for k, v in result['flags'].items() if v]}"
        )


if __name__ == "__main__":
    main()
