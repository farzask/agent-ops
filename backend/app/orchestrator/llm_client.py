"""Hand-written LLM client. No vendor SDK, no agent framework.

TECH_SPEC 8.3. This module owns request construction, response parsing, timeout
handling, retry with exponential backoff, and error classification.

The retry/backoff/timeout/classification machinery here is provider-agnostic and
real. Only the *transport* is currently mocked (``LLM_PROVIDER=mock``), which was
a deliberate choice: the orchestrator, retry paths, and failure demos all run at
zero API cost. Adding a real provider means implementing :class:`LLMProvider` -
it does not mean restructuring anything below.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error taxonomy (TECH_SPEC 3.2)
#
# The distinction between transient and permanent is the whole point: a
# transient error consumes a retry with backoff, a permanent one fails
# immediately rather than burning 13 seconds of backoff on a guaranteed 401.
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base for all LLM call failures."""


class TransientLLMError(LLMError):
    """Worth retrying: timeout, connection reset, 429, 5xx."""


class PermanentLLMError(LLMError):
    """Not worth retrying: 400, 401, 403, 404, malformed request."""


class MalformedResponseError(TransientLLMError):
    """Model returned something that isn't the JSON we asked for.

    Classed as transient because resampling genuinely often fixes it - the same
    prompt at temperature > 0 can produce valid JSON on the next attempt.
    """


class RetriesExhaustedError(LLMError):
    """All attempts failed. Carries the last underlying cause."""

    def __init__(self, attempts: int, last_error: BaseException) -> None:
        super().__init__(
            f"LLM call failed after {attempts} attempt(s): "
            f"{type(last_error).__name__}: {last_error}"
        )
        self.attempts = attempts
        self.last_error = last_error


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LLMRequest:
    """One completion request, in provider-neutral terms."""

    system: str
    user: str
    model: str
    max_tokens: int = 2048
    temperature: float = 0.2
    # Free-form tag used for logging and by the mock to shape its response.
    purpose: str = "generic"


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    # None when the provider doesn't report usage (the mock estimates).
    input_tokens: int | None = None
    output_tokens: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    """Transport. Implementations do exactly one thing: send and return text.

    They must raise :class:`TransientLLMError` or :class:`PermanentLLMError` and
    must not retry - retrying is :class:`LLMClient`'s job.
    """

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None


# ---------------------------------------------------------------------------
# Mock provider
#
# Deterministic when MOCK_SEED is set, so a test asserting on retry counts is
# reproducible. Failure and malformed rates drive the retry/failure demo the
# PRD asks for in 7.6 and 11.
# ---------------------------------------------------------------------------


class MockProvider(LLMProvider):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._rng = random.Random(settings.mock_seed)
        self._calls = 0

    @property
    def call_count(self) -> int:
        return self._calls

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self._calls += 1
        s = self._settings

        if s.mock_latency_ms:
            # Jittered so concurrent agents don't return in lockstep.
            jitter = self._rng.uniform(0.75, 1.25)
            await asyncio.sleep((s.mock_latency_ms / 1000.0) * jitter)

        if self._rng.random() < s.mock_failure_rate:
            raise TransientLLMError(
                "mock provider: simulated upstream 503 (MOCK_FAILURE_RATE)"
            )

        if self._rng.random() < s.mock_malformed_rate:
            return LLMResponse(
                text="Sure! Here is the plan you asked for -- but not as JSON.",
                model=request.model,
                raw={"mock": True, "malformed": True},
            )

        text = self._synthesize(request)
        return LLMResponse(
            text=text,
            model=request.model,
            input_tokens=_estimate_tokens(request.system + request.user),
            output_tokens=_estimate_tokens(text),
            raw={"mock": True},
        )

    def _synthesize(self, request: LLMRequest) -> str:
        """Produce a plausible, schema-valid response per agent purpose."""
        task = _first_line(request.user, limit=160)

        if request.purpose == "plan":
            return json.dumps(
                {
                    "subtasks": [
                        {
                            "index": 1,
                            "agent": "Worker",
                            "description": f"Research and gather key points for: {task}",
                        },
                        {
                            "index": 2,
                            "agent": "Worker",
                            "description": f"Draft the deliverable for: {task}",
                        },
                        {
                            "index": 3,
                            "agent": "Worker",
                            "description": "Tighten wording and check the draft answers the task",
                        },
                    ],
                    "reasoning": (
                        "Split into research, drafting, and revision so each step has "
                        "a single verifiable output."
                    ),
                }
            )

        if request.purpose == "clarify":
            return json.dumps(
                {
                    "ambiguities": [
                        "Target audience and tone were not specified.",
                        "No explicit length requirement was given.",
                    ],
                    "assumptions": [
                        "Audience is a technically literate general reader.",
                        "Target length is roughly 500 words unless stated otherwise.",
                    ],
                    "revised_plan_notes": (
                        "Plan is workable as written; assumptions recorded rather than "
                        "blocking on a human."
                    ),
                }
            )

        if request.purpose == "work":
            return json.dumps(
                {
                    "output": (
                        f"[mock worker output] Completed subtask: {task}\n\n"
                        "This is deterministic placeholder content produced by the mock "
                        "LLM provider. It exists so the orchestration, event stream, and "
                        "retry paths can be exercised end-to-end without an API key."
                    ),
                    "notes": "Subtask completed; no blockers encountered.",
                }
            )

        if request.purpose == "verify":
            return json.dumps(
                {
                    "approved": True,
                    "score": 0.86,
                    "feedback": (
                        "Output addresses the original task and is internally consistent. "
                        "Approved."
                    ),
                }
            )

        return json.dumps({"output": f"[mock] {task}"})


def _estimate_tokens(text: str) -> int:
    # Rough 4-chars-per-token heuristic. Only used for display.
    return max(1, len(text) // 4)


def _first_line(text: str, limit: int = 200) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:limit]


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "mock":
        return MockProvider(settings)
    # Unreachable while the config Literal allows only "mock". Kept so adding a
    # provider fails loudly here rather than silently falling back to the mock.
    raise PermanentLLMError(f"Unsupported LLM_PROVIDER: {settings.llm_provider!r}")


# ---------------------------------------------------------------------------
# JSON extraction
#
# Models wrap JSON in prose or fenced code blocks even when told not to. Trying
# a plain json.loads first, then progressively looser extraction, avoids
# spending a retry on a response that was actually usable.
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model response.

    Raises :class:`MalformedResponseError` if no JSON object can be recovered.
    """
    candidates: list[str] = [text.strip()]

    for match in _FENCE_RE.findall(text):
        candidates.append(match.strip())

    # Last resort: the outermost brace-balanced span.
    span = _outermost_object(text)
    if span is not None:
        candidates.append(span)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed

    raise MalformedResponseError(
        f"no JSON object found in response (first 200 chars: {text[:200]!r})"
    )


def _outermost_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

# Called as on_retry(attempt_number_1_indexed, delay_seconds, error).
RetryHook = Callable[[int, float, BaseException], Awaitable[None]]


class LLMClient:
    """Retry, backoff, timeout, and parsing around an :class:`LLMProvider`."""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        settings: Settings | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings or get_settings()
        self._provider = provider or build_provider(self._settings)
        # Injectable so tests assert on the backoff schedule without waiting.
        self._sleep = sleep

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    async def aclose(self) -> None:
        await self._provider.aclose()

    async def complete(
        self,
        request: LLMRequest,
        *,
        on_retry: RetryHook | None = None,
    ) -> LLMResponse:
        """Send ``request``, retrying transient failures with backoff.

        Raises :class:`PermanentLLMError` immediately on a permanent failure, or
        :class:`RetriesExhaustedError` once ``MAX_RETRIES`` attempts are used.
        """
        settings = self._settings
        last_error: BaseException | None = None

        for attempt in range(settings.max_retries):
            try:
                return await asyncio.wait_for(
                    self._provider.complete(request),
                    timeout=settings.llm_timeout_seconds,
                )
            except PermanentLLMError:
                # Permanent by definition - retrying cannot help.
                raise
            except (TransientLLMError, asyncio.TimeoutError) as exc:
                if isinstance(exc, asyncio.TimeoutError):
                    exc = TransientLLMError(
                        f"LLM call exceeded {settings.llm_timeout_seconds}s timeout"
                    )
                last_error = exc
                is_last = attempt == settings.max_retries - 1
                if is_last:
                    break
                delay = settings.backoff_delay(attempt)
                logger.warning(
                    "LLM transient failure (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    settings.max_retries,
                    delay,
                    exc,
                )
                if on_retry is not None:
                    await on_retry(attempt + 1, delay, exc)
                await self._sleep(delay)

        assert last_error is not None  # loop only breaks after setting it
        raise RetriesExhaustedError(settings.max_retries, last_error)

    async def complete_json(
        self,
        request: LLMRequest,
        *,
        on_retry: RetryHook | None = None,
    ) -> tuple[dict[str, Any], LLMResponse]:
        """Like :meth:`complete`, but also requires a parseable JSON object.

        A malformed response is a transient failure, so it consumes a retry from
        the same budget rather than a separate one - one unparseable reply and
        one 503 are both "this attempt produced nothing usable".
        """
        settings = self._settings
        last_error: BaseException | None = None

        for attempt in range(settings.max_retries):
            try:
                response = await asyncio.wait_for(
                    self._provider.complete(request),
                    timeout=settings.llm_timeout_seconds,
                )
                return extract_json(response.text), response
            except PermanentLLMError:
                raise
            except (TransientLLMError, asyncio.TimeoutError) as exc:
                if isinstance(exc, asyncio.TimeoutError):
                    exc = TransientLLMError(
                        f"LLM call exceeded {settings.llm_timeout_seconds}s timeout"
                    )
                last_error = exc
                if attempt == settings.max_retries - 1:
                    break
                delay = settings.backoff_delay(attempt)
                logger.warning(
                    "LLM JSON failure (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    settings.max_retries,
                    delay,
                    exc,
                )
                if on_retry is not None:
                    await on_retry(attempt + 1, delay, exc)
                await self._sleep(delay)

        assert last_error is not None
        raise RetriesExhaustedError(settings.max_retries, last_error)
