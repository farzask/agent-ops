"""Full pipeline runs against a mocked LLM client.

TECH_SPEC 10: "full pipeline run against a mocked LLM client, asserting correct
sequence of persisted agent_runs and emitted events."
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.redis_client import GLOBAL_EVENTS_CHANNEL
from app.models.db_models import AgentLog, AgentRun, AgentStatus, Job, JobStatus
from app.orchestrator.events import InMemoryEventPublisher
from app.orchestrator.llm_client import (
    LLMClient,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    MockProvider,
    PermanentLLMError,
    TransientLLMError,
)
from app.orchestrator.pipeline import Pipeline


def _events(publisher: InMemoryEventPublisher, event_type: str) -> list[dict]:
    """Events of one type, from the per-job channel only.

    Job-level events are deliberately fanned out to the global channel too (so
    the Job Queue View needn't subscribe per job), so counting every message
    would double them. test_events.py covers that fan-out explicitly.
    """
    out = []
    for channel, raw in publisher.messages:
        if channel == GLOBAL_EVENTS_CHANNEL:
            continue
        event = json.loads(raw)
        if event["event_type"] == event_type:
            out.append(event)
    return out


def _status_pairs(publisher: InMemoryEventPublisher) -> list[tuple[str, str]]:
    return [
        (e["payload"]["agent_name"], e["payload"]["new_status"])
        for e in _events(publisher, "agent_status_changed")
    ]


class ScriptedProvider(LLMProvider):
    """Returns canned responses by purpose, optionally failing first.

    Lets a test drive an exact pipeline path without depending on the mock
    provider's built-in content.
    """

    def __init__(
        self,
        responses: dict[str, list[str]],
        failures: dict[str, int] | None = None,
    ) -> None:
        self.responses = {k: list(v) for k, v in responses.items()}
        self.failures = dict(failures or {})
        self.calls: list[str] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request.purpose)

        remaining = self.failures.get(request.purpose, 0)
        if remaining > 0:
            self.failures[request.purpose] = remaining - 1
            raise TransientLLMError(f"scripted transient failure ({request.purpose})")

        queue = self.responses.get(request.purpose)
        if not queue:
            raise AssertionError(f"no scripted response for purpose {request.purpose!r}")
        text = queue.pop(0) if len(queue) > 1 else queue[0]
        return LLMResponse(text=text, model=request.model)


PLAN_2 = json.dumps(
    {
        "subtasks": [
            {"index": 1, "agent": "Worker", "description": "research"},
            {"index": 2, "agent": "Worker", "description": "draft"},
        ],
        "reasoning": "research then draft",
    }
)
CLARIFY_OK = json.dumps(
    {"ambiguities": ["tone"], "assumptions": ["technical reader"], "revised_plan_notes": "ok"}
)
WORK_OK = json.dumps({"output": "worker output", "notes": None})
VERIFY_APPROVE = json.dumps({"approved": True, "score": 0.9, "feedback": "approved"})
VERIFY_REJECT = json.dumps(
    {
        "approved": False,
        "score": 0.3,
        "feedback": "missing the cost analysis",
        "reject_subtask_index": 2,
    }
)


def _pipeline(
    session: AsyncSession,
    publisher: InMemoryEventPublisher,
    settings: Settings,
    provider: LLMProvider,
    sleep=None,
) -> Pipeline:
    client = LLMClient(provider, settings, sleep=sleep or (lambda _d: _noop()))
    return Pipeline(session, publisher, client, settings)


async def _noop() -> None:
    return None


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_completes_and_persists_ordered_runs(
    session: AsyncSession,
    publisher: InMemoryEventPublisher,
    settings: Settings,
    queued_job: Job,
) -> None:
    provider = ScriptedProvider(
        {
            "plan": [PLAN_2],
            "clarify": [CLARIFY_OK],
            "work": [WORK_OK],
            "verify": [VERIFY_APPROVE],
        }
    )
    result = await _pipeline(session, publisher, settings, provider).run(queued_job.id)

    assert result.status is JobStatus.COMPLETED
    assert result.failure_reason is None
    assert result.final_output == "worker output\n\nworker output"

    runs = (
        (
            await session.execute(
                select(AgentRun)
                .where(AgentRun.job_id == queued_job.id)
                .order_by(AgentRun.sequence_index)
            )
        )
        .scalars()
        .all()
    )
    # Supervisor -> Clarifier -> Worker-1 -> Worker-2 -> Verifier
    assert [(r.agent_name, r.sequence_index) for r in runs] == [
        ("Supervisor", 0),
        ("Clarifier", 1),
        ("Worker-1", 2),
        ("Worker-2", 3),
        ("Verifier", 4),
    ]
    assert all(r.status is AgentStatus.COMPLETED for r in runs)
    assert all(r.attempt_count == 1 for r in runs)
    assert all(r.duration_ms is not None for r in runs)

    await session.refresh(queued_job)
    assert queued_job.status is JobStatus.COMPLETED
    assert queued_job.completed_at is not None


@pytest.mark.asyncio
async def test_happy_path_emits_full_transition_sequence(
    session: AsyncSession,
    publisher: InMemoryEventPublisher,
    settings: Settings,
    queued_job: Job,
) -> None:
    provider = ScriptedProvider(
        {
            "plan": [PLAN_2],
            "clarify": [CLARIFY_OK],
            "work": [WORK_OK],
            "verify": [VERIFY_APPROVE],
        }
    )
    await _pipeline(session, publisher, settings, provider).run(queued_job.id)

    # Every agent goes queued -> running -> completed, in pipeline order.
    assert _status_pairs(publisher) == [
        ("Supervisor", "queued"),
        ("Supervisor", "running"),
        ("Supervisor", "completed"),
        ("Clarifier", "queued"),
        ("Clarifier", "running"),
        ("Clarifier", "completed"),
        ("Worker-1", "queued"),
        ("Worker-1", "running"),
        ("Worker-1", "completed"),
        ("Worker-2", "queued"),
        ("Worker-2", "running"),
        ("Worker-2", "completed"),
        ("Verifier", "queued"),
        ("Verifier", "running"),
        ("Verifier", "completed"),
    ]

    job_events = [e["payload"]["new_status"] for e in _events(publisher, "job_status_changed")]
    assert job_events == ["running", "completed"]


@pytest.mark.asyncio
async def test_assumptions_and_subtasks_are_logged(
    session: AsyncSession,
    publisher: InMemoryEventPublisher,
    settings: Settings,
    queued_job: Job,
) -> None:
    provider = ScriptedProvider(
        {
            "plan": [PLAN_2],
            "clarify": [CLARIFY_OK],
            "work": [WORK_OK],
            "verify": [VERIFY_APPROVE],
        }
    )
    await _pipeline(session, publisher, settings, provider).run(queued_job.id)

    messages = [e["payload"]["message"] for e in _events(publisher, "log_line")]
    assert any("Decomposed task into 2 subtask" in m for m in messages)
    assert any("Assumption recorded" in m and "technical reader" in m for m in messages)
    assert any("Ambiguity identified" in m for m in messages)
    assert any("Verifier approved" in m for m in messages)

    persisted = (await session.execute(select(AgentLog))).scalars().all()
    assert len(persisted) == len(messages), "every streamed log must be persisted"


# ---------------------------------------------------------------------------
# Technical retry (PRD 7.6 - visualized, not hidden)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_failure_emits_retrying_then_succeeds(
    session: AsyncSession,
    publisher: InMemoryEventPublisher,
    settings: Settings,
    queued_job: Job,
) -> None:
    provider = ScriptedProvider(
        {
            "plan": [PLAN_2],
            "clarify": [CLARIFY_OK],
            "work": [WORK_OK],
            "verify": [VERIFY_APPROVE],
        },
        failures={"plan": 1},  # Supervisor fails once, then succeeds
    )
    result = await _pipeline(session, publisher, settings, provider).run(queued_job.id)

    assert result.status is JobStatus.COMPLETED

    supervisor_states = [s for name, s in _status_pairs(publisher) if name == "Supervisor"]
    # The retry is observable: failed -> retrying -> running, not swallowed.
    assert supervisor_states == ["queued", "running", "failed", "retrying", "running", "completed"]

    run = (
        await session.execute(select(AgentRun).where(AgentRun.agent_name == "Supervisor"))
    ).scalar_one()
    assert run.attempt_count == 2
    assert run.status is AgentStatus.COMPLETED
    assert run.failure_reason is None, "a recovered run must not show a failure"

    messages = [e["payload"]["message"] for e in _events(publisher, "log_line")]
    assert any("Retrying in" in m for m in messages)


@pytest.mark.asyncio
async def test_retries_exhausted_fails_the_run_with_a_reason(
    session: AsyncSession,
    publisher: InMemoryEventPublisher,
    settings: Settings,
    queued_job: Job,
) -> None:
    provider = ScriptedProvider(
        {"plan": [PLAN_2], "clarify": [CLARIFY_OK], "work": [WORK_OK], "verify": [VERIFY_APPROVE]},
        failures={"plan": 99},
    )
    result = await _pipeline(session, publisher, settings, provider).run(queued_job.id)

    assert result.status is JobStatus.FAILED
    assert "retries exhausted" in (result.failure_reason or "")

    await session.refresh(queued_job)
    assert queued_job.status is JobStatus.FAILED
    assert queued_job.failure_reason is not None, "PRD 7.6: reason must be persisted"
    assert queued_job.completed_at is not None

    run = (
        await session.execute(select(AgentRun).where(AgentRun.agent_name == "Supervisor"))
    ).scalar_one()
    assert run.status is AgentStatus.FAILED
    assert run.attempt_count == settings.max_retries

    # The pipeline halts - no downstream agent should have been created.
    names = {r.agent_name for r in (await session.execute(select(AgentRun))).scalars()}
    assert names == {"Supervisor"}


@pytest.mark.asyncio
async def test_permanent_error_fails_immediately_without_retrying(
    session: AsyncSession,
    publisher: InMemoryEventPublisher,
    settings: Settings,
    queued_job: Job,
) -> None:
    class Unauthorized(LLMProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request: LLMRequest) -> LLMResponse:
            self.calls += 1
            raise PermanentLLMError("401 unauthorized")

    provider = Unauthorized()
    result = await _pipeline(session, publisher, settings, provider).run(queued_job.id)

    assert result.status is JobStatus.FAILED
    assert provider.calls == 1, "a 401 must not consume the retry budget"
    assert "permanent" in (result.failure_reason or "").lower()

    states = [s for _n, s in _status_pairs(publisher)]
    assert "retrying" not in states


# ---------------------------------------------------------------------------
# Verifier rework (separate budget from technical retries - TECH_SPEC 3.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_rejection_reworks_only_the_named_subtask(
    session: AsyncSession,
    publisher: InMemoryEventPublisher,
    settings: Settings,
    queued_job: Job,
) -> None:
    provider = ScriptedProvider(
        {
            "plan": [PLAN_2],
            "clarify": [CLARIFY_OK],
            "work": [WORK_OK],
            # Reject subtask 2 once, then approve.
            "verify": [VERIFY_REJECT, VERIFY_APPROVE],
        }
    )
    result = await _pipeline(session, publisher, settings, provider).run(queued_job.id)

    assert result.status is JobStatus.COMPLETED

    worker1 = [s for n, s in _status_pairs(publisher) if n == "Worker-1"]
    worker2 = [s for n, s in _status_pairs(publisher) if n == "Worker-2"]

    # Worker-1's output survived the rework cycle; only Worker-2 re-ran.
    assert worker1 == ["queued", "running", "completed"]
    assert worker2 == [
        "queued",
        "running",
        "completed",  # first pass
        "queued",
        "running",
        "completed",  # rework
    ]

    run2 = (
        await session.execute(select(AgentRun).where(AgentRun.agent_name == "Worker-2"))
    ).scalar_one()
    assert run2.attempt_count == 2

    # One node per agent, not a duplicate per rework cycle.
    runs = (await session.execute(select(AgentRun))).scalars().all()
    assert len(runs) == 5


@pytest.mark.asyncio
async def test_rework_does_not_consume_the_technical_retry_budget(
    session: AsyncSession,
    publisher: InMemoryEventPublisher,
    settings: Settings,
    queued_job: Job,
) -> None:
    """TECH_SPEC 3.2: rejections are counted separately from technical failures."""
    provider = ScriptedProvider(
        {
            "plan": [PLAN_2],
            "clarify": [CLARIFY_OK],
            "work": [WORK_OK],
            "verify": [VERIFY_REJECT, VERIFY_APPROVE],
        }
    )
    await _pipeline(session, publisher, settings, provider).run(queued_job.id)

    verifier = (
        await session.execute(select(AgentRun).where(AgentRun.agent_name == "Verifier"))
    ).scalar_one()

    assert verifier.rework_count == 1, "the rejection is a rework, tracked separately"
    assert verifier.status is AgentStatus.COMPLETED
    # No `retrying` state anywhere: a rejection is not a technical failure.
    assert "retrying" not in [s for _n, s in _status_pairs(publisher)]


@pytest.mark.asyncio
async def test_rework_limit_exhausted_fails_the_run(
    session: AsyncSession,
    publisher: InMemoryEventPublisher,
    settings: Settings,
    queued_job: Job,
) -> None:
    provider = ScriptedProvider(
        {
            "plan": [PLAN_2],
            "clarify": [CLARIFY_OK],
            "work": [WORK_OK],
            "verify": [VERIFY_REJECT],  # always rejects
        }
    )
    result = await _pipeline(session, publisher, settings, provider).run(queued_job.id)

    assert result.status is JobStatus.FAILED
    reason = result.failure_reason or ""
    assert "rework cycle" in reason
    assert "missing the cost analysis" in reason, "last feedback must be surfaced"

    await session.refresh(queued_job)
    assert queued_job.status is JobStatus.FAILED


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_plan_is_retried_then_fails(
    session: AsyncSession,
    publisher: InMemoryEventPublisher,
    settings: Settings,
    queued_job: Job,
) -> None:
    provider = ScriptedProvider({"plan": ["I'd be happy to help you plan that!"]})
    result = await _pipeline(session, publisher, settings, provider).run(queued_job.id)

    assert result.status is JobStatus.FAILED
    assert provider.calls.count("plan") == settings.max_retries


@pytest.mark.asyncio
async def test_already_running_job_is_not_run_twice(
    session: AsyncSession,
    publisher: InMemoryEventPublisher,
    settings: Settings,
    queued_job: Job,
) -> None:
    """Guards against double-consumption of a queue message."""
    from app.orchestrator.pipeline import PipelineFailure

    queued_job.status = JobStatus.RUNNING
    await session.commit()

    provider = ScriptedProvider({"plan": [PLAN_2]})
    with pytest.raises(PipelineFailure, match="expected queued"):
        await _pipeline(session, publisher, settings, provider).run(queued_job.id)


@pytest.mark.asyncio
async def test_unknown_job_id_raises(
    session: AsyncSession,
    publisher: InMemoryEventPublisher,
    settings: Settings,
) -> None:
    import uuid as _uuid

    from app.orchestrator.pipeline import PipelineFailure

    provider = ScriptedProvider({"plan": [PLAN_2]})
    with pytest.raises(PipelineFailure, match="not found"):
        await _pipeline(session, publisher, settings, provider).run(_uuid.uuid4())


@pytest.mark.asyncio
async def test_default_mock_provider_runs_end_to_end(
    session: AsyncSession,
    publisher: InMemoryEventPublisher,
    settings: Settings,
    queued_job: Job,
) -> None:
    """The shipped mock must produce a completing run with no scripting.

    This is the path the demo actually uses (PRD 9: >95% of demo runs complete).
    """
    result = await _pipeline(session, publisher, settings, MockProvider(settings)).run(
        queued_job.id
    )

    assert result.status is JobStatus.COMPLETED, result.failure_reason
    assert result.final_output

    runs = (
        (await session.execute(select(AgentRun).order_by(AgentRun.sequence_index))).scalars().all()
    )
    # The mock's canned plan has 3 subtasks.
    assert [r.agent_name for r in runs] == [
        "Supervisor",
        "Clarifier",
        "Worker-1",
        "Worker-2",
        "Worker-3",
        "Verifier",
    ]
    assert all(r.status is AgentStatus.COMPLETED for r in runs)
