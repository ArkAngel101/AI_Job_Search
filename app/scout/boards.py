"""Company boards per ATS, read from boards.yaml (owner-editable copy lives in /data)."""
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from app import config

ATS_NAMES = ("greenhouse", "lever", "ashby", "workable")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class Board:
    """One company's job board."""

    slug: str
    name: str


def boards_path() -> Path:
    """The editable boards file inside the data directory."""
    return config.DATA_DIR / "boards.yaml"


def ensure_boards_file() -> Path:
    """Copy the default boards.yaml into /data on first run; return the editable path."""
    path = boards_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(config.APP_ROOT / "boards.yaml", path)
    return path


def parse_boards(text: str) -> dict[str, list[Board]]:
    """Parse and validate boards YAML. Entries are slugs or {slug, name}. Raises ValueError."""
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("boards.yaml must map ATS names to lists of boards")
    result: dict[str, list[Board]] = {}
    for ats, entries in data.items():
        if ats not in ATS_NAMES:
            raise ValueError(f"Unknown ATS '{ats}' (allowed: {', '.join(ATS_NAMES)})")
        boards: list[Board] = []
        for entry in entries or []:
            slug, name = (entry, None) if isinstance(entry, str) else (entry.get("slug"), entry.get("name"))
            if not isinstance(slug, str) or not SLUG_RE.match(slug):
                raise ValueError(f"Invalid board slug under {ats}: {slug!r}")
            boards.append(Board(slug=slug, name=str(name) if name else slug))
        result[ats] = boards
    return result


def load_boards() -> dict[str, list[Board]]:
    """Load the owner's boards, creating the file from the default if needed."""
    return parse_boards(ensure_boards_file().read_text(encoding="utf-8"))
