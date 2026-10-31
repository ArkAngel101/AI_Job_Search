"""Shared Scout data types."""
from dataclasses import dataclass


@dataclass(slots=True)
class RawListing:
    """One job listing normalized from any source."""

    source: str
    source_id: str
    title: str
    company: str
    location: str
    description: str
    url: str
    created: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    contract_type: str | None = None
    board: str | None = None  # "ats/slug" for company boards, None for aggregators


class Notes(list[str]):
    """Human-readable notes from a fetch, plus the boards that were fetched completely."""

    def __init__(self, iterable: list[str] | None = None) -> None:
        super().__init__(iterable or [])
        self.completed_boards: set[str] = set()
