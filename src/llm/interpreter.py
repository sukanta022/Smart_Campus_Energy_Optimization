"""OpenAI-based operator-note interpreter.

Issues a single structured-output chat completion per request, with retries
via tenacity and a circuit breaker via pybreaker. Safe-failure behavior:
on any error (network, rate-limit, malformed output) the caller can fall
back to the deterministic regex parser in `fallback.py`.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)
from pydantic import BaseModel, Field
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.models import DirectiveInterpretation, OptimizeRequest

logger = logging.getLogger(__name__)


PROMPT_DIR = Path(__file__).parent / "prompts"


class InterpreterError(Exception):
    """Raised when the LLM cannot produce a usable interpretation."""


class LLMConfig(BaseModel):
    """Runtime configuration for the LLM client."""

    api_key: str = Field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    model: str = Field(default_factory=lambda: os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    timeout_seconds: float = Field(default_factory=lambda: float(os.environ.get("OPENAI_TIMEOUT_S", "3.0")))
    seed: int = Field(default_factory=lambda: int(os.environ.get("OPENAI_SEED", "42")))
    max_retries: int = Field(default_factory=lambda: int(os.environ.get("OPENAI_MAX_RETRIES", "2")))


def load_system_prompt() -> str:
    """Read the canonical system prompt from disk."""
    path = PROMPT_DIR / "system.txt"
    return path.read_text(encoding="utf-8")


class LLMInterpreter:
    """Async wrapper around the OpenAI chat.completions endpoint.

    The underlying ``AsyncOpenAI`` client is constructed lazily on the first
    ``interpret()`` call. This keeps the service bootable in environments
    without ``OPENAI_API_KEY`` (so the deterministic fallback parser path
    works) and avoids opening a credential socket during process startup.
    """

    def __init__(self, config: LLMConfig | None = None, *, client: AsyncOpenAI | None = None) -> None:
        self._config = config or LLMConfig()
        # Lazy import the schema builder so import-time stays light.
        from llm.schema import build_response_format

        self._response_format = build_response_format()
        self._system_prompt = load_system_prompt()
        # ``_client`` is built on first use so a missing API key only fails
        # when we actually try to talk to OpenAI. Caller code in the
        # orchestrator already handles that case by switching to the
        # deterministic regex fallback parser.
        self._client: AsyncOpenAI | None = client
        self._client_locked = client is not None

    def _get_client(self) -> AsyncOpenAI:
        """Return the OpenAI client, constructing it on first access."""
        if self._client is not None:
            return self._client
        if not self._config.api_key:
            raise InterpreterError(
                "OPENAI_API_KEY is not configured; cannot construct the OpenAI client. "
                "Either set the OPENAI_API_KEY environment variable or enable "
                "GRIDWISE_ENABLE_FALLBACK_PARSER=true to use the deterministic regex parser."
            )
        self._client = AsyncOpenAI(
            api_key=self._config.api_key,
            timeout=httpx.Timeout(self._config.timeout_seconds),
            max_retries=0,  # we drive retries ourselves for visibility
        )
        self._client_locked = True
        return self._client

    async def interpret(self, request: OptimizeRequest) -> list[DirectiveInterpretation]:
        """Interpret every operator note and return one DirectiveInterpretation per note."""
        from llm.validator import safe_validate_directives

        messages = self._build_messages(request)
        raw = await self._call_chat(messages)
        parsed = self._parse_payload(raw, expected_count=len(request.operator_notes))
        return safe_validate_directives(parsed, n_notes=len(request.operator_notes))

    # ---- internals --------------------------------------------------------

    def _build_messages(self, request: OptimizeRequest) -> list[dict[str, Any]]:
        user_payload = {
            "scenario_id": request.scenario_id,
            "operator_notes": request.operator_notes,
        }
        return [
            {"role": "system", "content": self._system_prompt},
            {
                "role": "user",
                "content": (
                    "Interpret the operator notes below for scenario "
                    f"{request.scenario_id!r}. Return the JSON object "
                    "described by your response_format schema.\n\n"
                    f"OPERATOR_NOTES = {json.dumps(user_payload, ensure_ascii=False)}"
                ),
            },
        ]

    async def _call_chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Issue the chat completion with bounded retries."""
        client = self._get_client()  # may raise InterpreterError on missing key
        attempt = 0
        try:
            async for attempt_ctx in AsyncRetrying(
                stop=stop_after_attempt(self._config.max_retries + 1),
                wait=wait_exponential(multiplier=0.4, min=0.4, max=2.0),
                retry=retry_if_exception_type((APIConnectionError, APITimeoutError, RateLimitError)),
                reraise=True,
            ):
                with attempt_ctx:
                    attempt += 1
                    response = await client.chat.completions.create(
                        model=self._config.model,
                        messages=messages,
                        temperature=0,
                        seed=self._config.seed,
                        response_format=self._response_format,
                    )
                    return self._extract_message(response)
        except RetryError as e:
            raise InterpreterError(f"LLM retries exhausted: {e}") from e
        except AuthenticationError as e:
            raise InterpreterError(f"OpenAI authentication failed: {e}") from e
        except BadRequestError as e:
            raise InterpreterError(f"OpenAI rejected the request: {e}") from e
        except (APIConnectionError, APITimeoutError, RateLimitError) as e:
            raise InterpreterError(f"OpenAI transport error after {attempt} attempt(s): {e}") from e

    @staticmethod
    def _extract_message(response: Any) -> dict[str, Any]:
        if not response.choices:
            raise InterpreterError("OpenAI returned no choices")
        content = response.choices[0].message.content
        if not content:
            raise InterpreterError("OpenAI returned empty content")
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise InterpreterError(f"LLM returned non-JSON content: {e}") from e

    @staticmethod
    def _parse_payload(payload: dict[str, Any], *, expected_count: int) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            raise InterpreterError("LLM payload is not a JSON object")
        if "directives" not in payload:
            raise InterpreterError("LLM payload missing 'directives' field")
        directives = payload["directives"]
        if not isinstance(directives, list):
            raise InterpreterError("LLM 'directives' field is not an array")
        if len(directives) != expected_count:
            raise InterpreterError(
                f"LLM returned {len(directives)} directives, expected {expected_count}"
            )
        return directives
