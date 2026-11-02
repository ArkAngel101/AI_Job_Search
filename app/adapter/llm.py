"""The only module that calls an LLM: Groq's OpenAI-compatible chat API, using strict JSON-schema output.

Every call: budget check first, then one request, then usage logged to llm_usage and the activity log.
Error messages shown to the owner never contain request details, URLs, or keys.
"""
import os
from types import SimpleNamespace
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app import db

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"  # supports strict JSON-schema output
REQUEST_TIMEOUT = httpx.Timeout(180.0, connect=15.0)
CHARS_PER_TOKEN = 3.5  # Groq has no token-counting endpoint, so estimates are approximate

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """An LLM call failed. The message is safe to show the owner."""


class NotConfigured(LLMError):
    """No API key is set."""


class BudgetExceeded(LLMError):
    """The monthly spend cap would be exceeded (or cannot be checked)."""


class ProviderError(Exception):
    """The provider answered with an error (status 0 means it could not be reached)."""

    def __init__(self, status: int, kind: str | None = None, request_id: str | None = None) -> None:
        super().__init__(f"provider error {status}")
        self.status, self.kind, self.request_id = status, kind, request_id


def model_name() -> str:
    """GROQ_MODEL (or the older CLAUDE_MODEL name), ignoring leftover Claude model names; else the default."""
    for variable in ("GROQ_MODEL", "CLAUDE_MODEL"):
        value = os.environ.get(variable, "").strip()
        if value and not value.lower().startswith("claude"):
            return value
    return DEFAULT_MODEL


def model_from_env() -> bool:
    """True if the model comes from .env rather than the default."""
    return model_name() != DEFAULT_MODEL or any(
        os.environ.get(v, "").strip() == DEFAULT_MODEL for v in ("GROQ_MODEL", "CLAUDE_MODEL"))


def api_key_set() -> bool:
    """True if GROQ_API_KEY is set (the key itself is never exposed)."""
    return bool(os.environ.get("GROQ_API_KEY", "").strip())


# --- the Groq client ---------------------------------------------------------------------------

def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The pydantic model's JSON schema in Groq's strict form: every property required, no extra properties."""
    schema = model.model_json_schema()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("title", None)
            node.pop("default", None)
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema


class GroqClient:
    """Minimal client exposing `messages.parse(...)`, the one call this app needs."""

    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(timeout=REQUEST_TIMEOUT, transport=transport)
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.messages = self

    def parse(self, *, model: str, max_tokens: int, system: str, messages: list[dict[str, str]],
              output_format: type[T], **_ignored: Any) -> Any:
        """One chat completion whose answer must match `output_format`; returns the parsed model and usage."""
        body = {
            "model": model, "max_completion_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": output_format.__name__, "strict": True, "schema": strict_schema(output_format)}},
        }
        try:
            response = self._http.post(GROQ_URL, json=body, headers=self._headers)
        except httpx.HTTPError:
            raise ProviderError(0, "connection") from None
        if response.status_code != 200:
            try:
                error = response.json().get("error", {})
            except ValueError:
                error = {}
            kind = error.get("type") if isinstance(error, dict) else None
            raise ProviderError(response.status_code, kind, response.headers.get("x-request-id"))
        try:
            data = response.json()
            choice = data["choices"][0]
            content = (choice.get("message") or {}).get("content")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError(response.status_code, "unexpected_response") from None
        usage = data.get("usage") or {}
        finish = choice.get("finish_reason")
        return SimpleNamespace(
            parsed_output=output_format.model_validate_json(content) if content else None,
            stop_reason={"length": "max_tokens", "stop": "end_turn"}.get(finish, finish),
            usage=SimpleNamespace(input_tokens=int(usage.get("prompt_tokens") or 0),
                                  output_tokens=int(usage.get("completion_tokens") or 0)),
        )


def get_client() -> GroqClient:
    """A Groq client using GROQ_API_KEY from the environment."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        raise NotConfigured("GROQ_API_KEY is not set. Add it to .env and restart.")
    return GroqClient(key)


def describe_provider_error(exc: ProviderError) -> str:
    """A safe explanation of a provider error: known cases in words, otherwise only status, type, request id."""
    if exc.status == 0:
        return "Could not reach the Groq API."
    if exc.status in (401, 403):
        return "Groq rejected the API key (check GROQ_API_KEY in .env)."
    if exc.status == 429:
        return "Groq is rate limiting requests. Try again in a minute."
    extra = ", ".join(str(x) for x in (exc.kind, f"request {exc.request_id}" if exc.request_id else None) if x)
    return f"The Groq API returned an error ({exc.status}{', ' + extra if extra else ''}). Details are not shown because they can quote your CV."


# --- prices, cap, estimates ------------------------------------------------------------------

def get_prices() -> tuple[float, float] | None:
    """(input, output) dollars per million tokens as entered in Settings, or None if not set."""
    with db.connect() as conn:
        prices = db.get_setting(conn, "claude_prices")
    try:
        return float(prices["input_per_mtok"]), float(prices["output_per_mtok"])
    except (TypeError, KeyError, ValueError):
        return None


def cost_limits_disabled() -> bool:
    """True if the owner switched cost tracking and the spend cap off in Settings (the values are kept)."""
    with db.connect() as conn:
        return bool(db.get_setting(conn, "cost_limits_disabled", False))


def cost_usd(input_tokens: int, output_tokens: int) -> float | None:
    """Dollar cost for the given tokens at the Settings prices, or None without prices."""
    prices = get_prices()
    return None if prices is None else (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000


def estimate(system: str, user: str, expected_output_tokens: int) -> dict[str, Any]:
    """Estimated tokens and cost of a call, shown to the owner before it is made (always approximate)."""
    input_tokens = int((len(system) + len(user)) / CHARS_PER_TOKEN) + 1
    disabled = cost_limits_disabled()
    return {
        "model": model_name(), "input_tokens": input_tokens, "output_tokens_estimate": expected_output_tokens,
        "cost_usd": None if disabled else cost_usd(input_tokens, expected_output_tokens), "basis": "approximate",
        "cost_disabled": disabled,
    }


def check_budget(estimated_cost: float | None) -> None:
    """Hard stop: refuse a call that would push this month's spend past the cap set in Settings."""
    if cost_limits_disabled():
        return
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


# --- the call --------------------------------------------------------------------------------------

def structured_call(
    *, purpose: str, system: str, user: str, schema: type[T], expected_output_tokens: int,
    job_key: str | None = None, max_tokens: int = 16000,
) -> T:
    """Make one LLM call returning an instance of `schema`; log usage and activity."""
    model = model_name()
    est_in = int((len(system) + len(user)) / CHARS_PER_TOKEN) + 1
    check_budget(cost_usd(est_in, expected_output_tokens))
    try:
        response = get_client().messages.parse(
            model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
    except ProviderError as exc:
        raise _failed(purpose, model, describe_provider_error(exc)) from None
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
