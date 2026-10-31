"""Shared test doubles. Claude is never called for real in any test."""
from types import SimpleNamespace
from typing import Any

from app.adapter.schema import CVStructured, TailoredCV
from test_phase7 import good_structured


def tailored_from(base: CVStructured) -> TailoredCV:
    """A faithful rewrite of a built base CV: same facts, bullets reordered."""
    return TailoredCV(
        headline="Machine Learning Intern with a focus on NLP", summary=base.summary,
        experience=[{"company": e.company, "role": e.role, "start": e.start, "bullets": list(reversed(e.bullets))} for e in base.experience])


class ComboClient:
    """Fake Anthropic client that answers both the CV build and the tailoring call."""

    def __init__(self) -> None:
        self.messages = self
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        schema = kwargs["output_format"]
        result = good_structured() if schema is CVStructured else tailored_from(good_structured())
        assert isinstance(result, schema)
        return SimpleNamespace(parsed_output=result, stop_reason="end_turn", usage=SimpleNamespace(input_tokens=2000, output_tokens=600))

    def count_tokens(self, **kwargs: Any) -> Any:
        return SimpleNamespace(input_tokens=1800)
