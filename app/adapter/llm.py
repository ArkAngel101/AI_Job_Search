"""The only module that calls the Anthropic API.

Every call: budget check first, then one request, then usage logged to llm_usage and the activity log.
Error messages shown to the owner never contain request details, URLs, or keys.
"""
import os
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from app import db

DEFAULT_MODEL = "claude-sonnet-5-5"
EFFORT = "medium"
REQUEST_TIMEOUT_SECONDS = 180.0
CHARS_PER_TOKEN = 3.5  # rough figure used only when exact counting is unavailable

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """A Claude call failed. The message is safe to show the owner."""


class NotConfigured(LLMError):
    """No API key is set."""


class BudgetExceeded(LLMError):
    """The monthly spend cap would be exceeded (or cannot be checked)."""


def model_name() -> str:
    """The model from CLAUDE_MODEL, defaulting to Claude Sonnet 5.5."""
    return os.environ.get("CLAUDE_MODEL", "").strip() or DEFAULT_MODEL


def get_client() -> anthropic.Anthropic:
    """An Anthropic client using ANTHROPIC_API_KEY from the environment."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise NotConfigured("ANTHROPIC_API_KEY is not set. Add it to .env and restart.")
    return anthropic.Anthropic(api_key=key, timeout=REQUEST_TIMEOUT_SECONDS)


# --- prices, cap, estimates ------------------------------------------------------

def get_prices() -> tuple[float, float] | None:
    """(input, output) dollars per million tokens as entered in Settings, or None if not set."""
    with db.connect() as conn:
        prices = db.get_setting(conn, "claude_prices")
    try:
        return float(prices["input_per_mtok"]), float(prices["output_per_mtok"])
    except (TypeError, KeyError, ValueError):
        return None


def cost_usd(input_tokens: int, output_tokens: int) -> float | None:
    """Dollar cost for the given tokens at the Settings prices, or None without prices."""
    prices = get_prices()
    return None if prices is None else (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000


def estimate(system: str, user: str, expected_output_tokens: int) -> dict[str, Any]:
    """Estimated tokens and cost of a call, shown to the owner before it is made."""
    basis = "counted"
    try:
        counted = get_client().messages.count_tokens(
            model=model_name(), system=system, messages=[{"role": "user", "content": user}]
        )
        input_tokens = int(counted.input_tokens)
    except (LLMError, anthropic.APIError):
        input_tokens = int((len(system) + len(user)) / CHARS_PER_TOKEN) + 1
        basis = "approximate"
    return {
        "model": model_name(), "input_tokens": input_tokens, "output_tokens_estimate": expected_output_tokens,
        "cost_usd": cost_usd(input_tokens, expected_output_tokens), "basis": basis,
    }


def check_budget(estimated_cost: float | None) -> None:
    """Hard stop: refuse a call that would push this month's spend past the cap set in Settings."""
    with db.connect() as conn:
        cap = db.get_setting(conn, "monthly_cap_usd")
        spent = db.month_spend(conn)
    if not cap or float(cap) <= 0:
        return
    if get_prices() is None or estimated_cost is None:
        raise BudgetExceeded("A monthly spend cap is set, but token prices are missing in Settings, so it cannot be checked.")
    if spent + estimated_cost > float(cap):
        raise BudgetExceeded(
            f"Monthly cap of ${float(cap):.2f} would be exceeded (spent ${spent:.2f}, this call about ${estimated_cost:.2f})."
        )


# --- the call --------------------------------------------------------------------

def structured_call(
    *, purpose: str, system: str, user: str, schema: type[T], expected_output_tokens: int,
    job_key: str | None = None, max_tokens: int = 16000,
) -> T:
    """Make one Claude call returning an instance of `schema`; log usage and activity."""
    model = model_name()
    est_in = int((len(system) + len(user)) / CHARS_PER_TOKEN) + 1
    check_budget(cost_usd(est_in, expected_output_tokens))
    try:
        response = get_client().messages.parse(
            model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}],
            output_format=schema, output_config={"effort": EFFORT},
        )
    except anthropic.AuthenticationError:
        raise _failed(purpose, model, "Anthropic rejected the API key.") from None
    except anthropic.RateLimitError:
        raise _failed(purpose, model, "Anthropic is rate limiting requests. Try again in a minute.") from None
    except anthropic.APIConnectionError:
        raise _failed(purpose, model, "Could not reach the Anthropic API.") from None
    except anthropic.APIStatusError as exc:
        raise _failed(purpose, model, f"The Anthropic API returned an error ({exc.status_code}).") from None
    except ValidationError:
        raise _failed(purpose, model, "The model's answer did not match the expected format.") from None

    usage = getattr(response, "usage", None)
    in_tokens, out_tokens = int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)
    actual_cost = cost_usd(in_tokens, out_tokens)
    with db.connect() as conn:
        db.record_llm_usage(conn, job_key, model, in_tokens, out_tokens, actual_cost or 0.0)
    if response.stop_reason == "refusal":
        raise _failed(purpose, model, "The model declined this request.")
    if response.stop_reason == "max_tokens":
        raise _failed(purpose, model, "The answer was cut off (the CV may be too long).")
    parsed = response.parsed_output
    if parsed is None:
        raise _failed(purpose, model, "The model's answer could not be read.")
    db.log_activity("cv-adapter", f"{purpose}: {in_tokens} in, {out_tokens} out tokens", "completed", model)
    return parsed


def _failed(purpose: str, model: str, message: str) -> LLMError:
    db.log_activity("cv-adapter", f"{purpose} failed: {message}", "failed", model)
    return LLMError(message)
