"""LLM client: retry/backoff schedule, error classification, JSON extraction.

TECH_SPEC 10 requires unit coverage of the retry/backoff logic.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import Settings
from app.orchestrator.llm_client import (
    LLMClient,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    MalformedResponseError,
    MockProvider,
    PermanentLLMError,
    RetriesExhaustedError,
    TransientLLMError,
    extract_json,
)
from tests.conftest import RecordingSleep


def _request() -> LLMRequest:
    return LLMRequest(system="sys", user="usr", model="mock-small", purpose="plan")


class FlakyProvider(LLMProvider):
    """Fails ``fail_times`` times, then succeeds."""

    def __init__(self, fail_times: int, error: Exception | None = None) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self._error = error or TransientLLMError("simulated 503")

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self._error
        return LLMResponse(text='{"subtasks": [], "ok": true}', model=request.model)


class HangingProvider(LLMProvider):
    async def complete(self, request: LLMRequest) -> LLMResponse:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Backoff schedule
# ---------------------------------------------------------------------------


def test_backoff_schedule_is_1_3_9(settings: Settings) -> None:
    """TECH_SPEC 3.2 specifies exponential backoff of 1s, 3s, 9s."""
    assert settings.backoff_delay(0) == pytest.approx(1.0)
    assert settings.backoff_delay(1) == pytest.approx(3.0)
    assert settings.backoff_delay(2) == pytest.approx(9.0)


@pytest.mark.asyncio
async def test_transient_failure_retries_with_backoff(
    settings: Settings, recording_sleep: RecordingSleep
) -> None:
    provider = FlakyProvider(fail_times=2)
    client = LLMClient(provider, settings, sleep=recording_sleep)

    response = await client.complete(_request())

    assert provider.calls == 3, "should have retried twice then succeeded"
    assert recording_sleep.delays == [1.0, 3.0], "backoff must be 1s then 3s"
    assert response.model == "mock-small"


@pytest.mark.asyncio
async def test_retries_exhausted_raises_with_attempt_count(
    settings: Settings, recording_sleep: RecordingSleep
) -> None:
    provider = FlakyProvider(fail_times=99)
    client = LLMClient(provider, settings, sleep=recording_sleep)

    with pytest.raises(RetriesExhaustedError) as excinfo:
        await client.complete(_request())

    assert excinfo.value.attempts == settings.max_retries == 3
    assert provider.calls == 3, "exactly max_retries attempts, no more"
    # Only two sleeps for three attempts - no pointless wait after the last one.
    assert recording_sleep.delays == [1.0, 3.0]
    assert isinstance(excinfo.value.last_error, TransientLLMError)


@pytest.mark.asyncio
async def test_permanent_error_is_not_retried(
    settings: Settings, recording_sleep: RecordingSleep
) -> None:
    """A 401 will never succeed - burning 13s of backoff on it is a bug."""
    provider = FlakyProvider(fail_times=99, error=PermanentLLMError("401 unauthorized"))
    client = LLMClient(provider, settings, sleep=recording_sleep)

    with pytest.raises(PermanentLLMError):
        await client.complete(_request())

    assert provider.calls == 1, "permanent errors must fail on the first attempt"
    assert recording_sleep.delays == []


@pytest.mark.asyncio
async def test_on_retry_hook_receives_attempt_delay_and_error(
    settings: Settings, recording_sleep: RecordingSleep
) -> None:
    """The pipeline uses this hook to make retries visible in the UI."""
    provider = FlakyProvider(fail_times=2)
    client = LLMClient(provider, settings, sleep=recording_sleep)
    seen: list[tuple[int, float, str]] = []

    async def hook(attempt: int, delay: float, error: BaseException) -> None:
        seen.append((attempt, delay, type(error).__name__))

    await client.complete(_request(), on_retry=hook)

    assert seen == [
        (1, 1.0, "TransientLLMError"),
        (2, 3.0, "TransientLLMError"),
    ]


@pytest.mark.asyncio
async def test_timeout_is_classified_transient_and_retried() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://unused/unused",
        llm_timeout_seconds=0.01,
        max_retries=2,
        mock_latency_ms=0,
    )
    sleep = RecordingSleep()
    client = LLMClient(HangingProvider(), settings, sleep=sleep)

    with pytest.raises(RetriesExhaustedError) as excinfo:
        await client.complete(_request())

    assert isinstance(excinfo.value.last_error, TransientLLMError)
    assert "timeout" in str(excinfo.value.last_error).lower()
    assert sleep.delays == [1.0]


# ---------------------------------------------------------------------------
# JSON handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"approved": true}',
        '```json\n{"approved": true}\n```',
        '```\n{"approved": true}\n```',
        'Sure! Here you go:\n{"approved": true}\nHope that helps.',
        '{"approved": true, "feedback": "has a } brace in a string"}',
    ],
)
def test_extract_json_recovers_object(text: str) -> None:
    """Models wrap JSON in prose and fences even when told not to."""
    assert extract_json(text)["approved"] is True


@pytest.mark.parametrize(
    "text",
    ["", "no json at all", "[1, 2, 3]", "{not valid json}", "{unclosed"],
)
def test_extract_json_rejects_unrecoverable(text: str) -> None:
    with pytest.raises(MalformedResponseError):
        extract_json(text)


@pytest.mark.asyncio
async def test_malformed_response_consumes_a_retry(
    settings: Settings, recording_sleep: RecordingSleep
) -> None:
    """An unparseable reply and a 503 both mean 'this attempt produced nothing'."""

    class ProseProvider(LLMProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request: LLMRequest) -> LLMResponse:
            self.calls += 1
            if self.calls < 3:
                return LLMResponse(text="I'd be happy to help!", model=request.model)
            return LLMResponse(text='{"approved": true}', model=request.model)

    provider = ProseProvider()
    client = LLMClient(provider, settings, sleep=recording_sleep)

    data, _ = await client.complete_json(_request())

    assert data == {"approved": True}
    assert provider.calls == 3
    assert recording_sleep.delays == [1.0, 3.0]


@pytest.mark.asyncio
async def test_complete_json_exhausts_on_persistent_prose(
    settings: Settings, recording_sleep: RecordingSleep
) -> None:
    class AlwaysProse(LLMProvider):
        async def complete(self, request: LLMRequest) -> LLMResponse:
            return LLMResponse(text="never JSON", model=request.model)

    client = LLMClient(AlwaysProse(), settings, sleep=recording_sleep)

    with pytest.raises(RetriesExhaustedError) as excinfo:
        await client.complete_json(_request())

    assert isinstance(excinfo.value.last_error, MalformedResponseError)


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_provider_returns_valid_json_per_purpose(
    settings: Settings,
) -> None:
    client = LLMClient(MockProvider(settings), settings)
    for purpose in ("plan", "clarify", "work", "verify"):
        data, _ = await client.complete_json(
            LLMRequest(system="s", user="u", model="m", purpose=purpose)
        )
        assert isinstance(data, dict) and data, f"{purpose} produced no object"


@pytest.mark.asyncio
async def test_mock_seed_makes_failures_reproducible() -> None:
    """MOCK_SEED must give identical behaviour across runs, or a test asserting
    on retry counts is flaky."""

    def build() -> MockProvider:
        return MockProvider(
            Settings(
                database_url="postgresql+asyncpg://unused/unused",
                mock_latency_ms=0,
                mock_failure_rate=0.5,
                mock_seed=42,
            )
        )

    async def outcomes(provider: MockProvider) -> list[bool]:
        results = []
        for _ in range(12):
            try:
                await provider.complete(_request())
                results.append(True)
            except TransientLLMError:
                results.append(False)
        return results

    assert await outcomes(build()) == await outcomes(build())


@pytest.mark.asyncio
async def test_mock_failure_rate_one_always_fails() -> None:
    provider = MockProvider(
        Settings(
            database_url="postgresql+asyncpg://unused/unused",
            mock_latency_ms=0,
            mock_failure_rate=1.0,
            mock_seed=7,
        )
    )
    with pytest.raises(TransientLLMError):
        await provider.complete(_request())
