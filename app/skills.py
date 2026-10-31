"""AI/ML skill vocabulary from skills.yaml (read-only, shipped with the app)."""
from functools import lru_cache

import yaml

from app import config


@lru_cache(maxsize=1)
def _load() -> dict[str, list[str]]:
    data = yaml.safe_load((config.APP_ROOT / "skills.yaml").read_text(encoding="utf-8")) or {}
    return {key: [str(item) for item in data.get(key) or []] for key in ("default_scoring_skills", "vocabulary")}


def default_scoring_skills() -> list[str]:
    """Skills seeded into the profile until a CV is uploaded."""
    return list(_load()["default_scoring_skills"])


def vocabulary() -> list[str]:
    """Every skill term Job Reader looks for."""
    return list(_load()["vocabulary"])
