"""Event emission and state machine enforcement.

TECH_SPEC 10 requires unit coverage of pipeline state machine transitions and
event emission correctness.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis_client import GLOBAL_EVENTS_CHANNEL, job_events_channel
from app.models.db_models import (
    AgentLog,
    AgentRun,
    AgentStatus,
    Job,
    JobStatus,
    LogLevel,
)
from app.orchestrator.events import (
    LEGAL_AGENT_TRANSITIONS,
    EventEmitter,
    IllegalTransitionError,
    InMemoryEventPublisher,
    assert_legal_agent_transition,
    assert_legal_job_transition,
    reconcile_orphaned_jobs,
)

ALL_AGENT_STATUSES = list(AgentStatus)


async def _make_run(session: AsyncSession, job: Job, index: int = 0) -> AgentRun:
    run = AgentRun(
        job_id=job.id,
        agent_name="Supervisor",
        sequence_index=index,
        status=AgentStatus.IDLE,
        attempt_count=0,
        rework_count=0,
        input_payload={},
    )
    session.add(run)
    await session.commit()
    return run


# ---------------------------------------------------------------------------
# State machine (TECH_SPEC 3.1)
# ---------------------------------------------------------------------------


def test_legal_agent_transitions_match_spec() -> None:
    assert LEGAL_AGENT_TRANSITIONS[AgentStatus.IDLE] == frozenset({AgentStatus.QUEUED})
    assert LEGAL_AGENT_TRANSITIONS[AgentStatus.QUEUED] == frozenset(
        {AgentStatus.RUNNING}
    )
    assert LEGAL_AGENT_TRANSITIONS[AgentStatus.RUNNING] == frozenset(
        {AgentStatus.COMPLETED, AgentStatus.FAILED}
    )
    assert AgentStatus.RETRYING in LEGAL_AGENT_TRANSITIONS[AgentStatus.FAILED]
    assert LEGAL_AGENT_TRANSITIONS[AgentStatus.RETRYING] == frozenset(
        {AgentStatus.RUNNING}
    )


def test_idle_cannot_jump_straight_to_running() -> None:
    """Skipping `queued` would make the diagram lie about queue depth."""
    with pytest.raises(IllegalTransitionError):
        assert_legal_agent_transition(AgentStatus.IDLE, AgentStatus.RUNNING)


def test_completed_is_terminal_except_for_rework() -> None:
    # Rework re-queues a completed agent; nothing else is allowed.
    assert_legal_agent_transition(AgentStatus.COMPLETED, AgentStatus.QUEUED)
    for target in (AgentStatus.RUNNING, AgentStatus.FAILED, AgentStatus.RETRYING):
        with pytest.raises(IllegalTransitionError):
            assert_legal_agent_transition(AgentStatus.COMPLETED, target)


def test_terminal_job_statuses_allow_no_transition() -> None:
    for terminal in (JobStatus.COMPLETED, JobStatus.FAILED):
        for target in JobStatus:
            with pytest.raises(IllegalTransitionError):
                assert_legal_job_transition(terminal, target)


def test_no_agent_transition_map_entry_is_missing() -> None:
    """Every status needs an entry, or a legal transition raises by accident."""
    for status in ALL_AGENT_STATUSES:
        assert status in LEGAL_AGENT_TRANSITIONS


# ---------------------------------------------------------------------------
# Emission: persist AND publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_persists_row_and_publishes(
    session: AsyncSession, publisher: InMemoryEventPublisher, queued_job: Job
) -> None:
    emitter = EventEmitter(session, publisher, queued_job.id)

    await emitter.log("Decomposed task into 3 subtasks", agent_name="Supervisor")

    rows = (await session.execute(select(AgentLog))).scalars().all()
    assert len(rows) == 1, "log must be persisted, not only published"
    assert rows[0].message == "Decomposed task into 3 subtasks"
    assert rows[0].agent_name == "Supervisor"

    assert len(publisher.messages) == 1
    channel, raw = publisher.messages[0]
    assert channel == job_events_channel(str(queued_job.id))

    event = json.loads(raw)
    assert event["event_type"] == "log_line"
    assert event["job_id"] == str(queued_job.id)
    assert event["payload"]["message"] == "Decomposed task into 3 subtasks"
    assert event["payload"]["level"] == "info"
    # The frontend keys log rows by this id; without it React mis-diffs.
    assert event["payload"]["log_id"] == str(rows[0].id)


@pytest.mark.asyncio
async def test_agent_status_change_publishes_previous_and_new(
    session: AsyncSession, publisher: InMemoryEventPublisher, queued_job: Job
) -> None:
    run = await _make_run(session, queued_job)
    emitter = EventEmitter(session, publisher, queued_job.id)

    await emitter.agent_status(run, AgentStatus.QUEUED)
    await emitter.agent_status(run, AgentStatus.RUNNING)

    payloads = [json.loads(raw)["payload"] for _, raw in publisher.messages]
    assert payloads[0]["previous_status"] == "idle"
    assert payloads[0]["new_status"] == "queued"
    assert payloads[1]["previous_status"] == "queued"
    assert payloads[1]["new_status"] == "running"
    assert payloads[1]["agent_name"] == "Supervisor"
    assert payloads[1]["sequence_index"] == 0


@pytest.mark.asyncio
async def test_running_increments_attempt_count_and_sets_started_at(
    session: AsyncSession, publisher: InMemoryEventPublisher, queued_job: Job
) -> None:
    run = await _make_run(session, queued_job)
    emitter = EventEmitter(session, publisher, queued_job.id)

    await emitter.agent_status(run, AgentStatus.QUEUED)
    await emitter.agent_status(run, AgentStatus.RUNNING)
    assert run.attempt_count == 1
    first_start = run.started_at
    assert first_start is not None

    # A retry cycle: each RUNNING entry is one attempt.
    await emitter.agent_status(run, AgentStatus.FAILED, failure_reason="503")
    await emitter.agent_status(run, AgentStatus.RETRYING)
    await emitter.agent_status(run, AgentStatus.RUNNING)

    assert run.attempt_count == 2
    assert run.started_at == first_start, "started_at is the first attempt's start"


@pytest.mark.asyncio
async def test_completed_clears_stale_failure_reason(
    session: AsyncSession, publisher: InMemoryEventPublisher, queued_job: Job
) -> None:
    """A run that failed then succeeded must not still show a failure reason."""
    run = await _make_run(session, queued_job)
    emitter = EventEmitter(session, publisher, queued_job.id)

    await emitter.agent_status(run, AgentStatus.QUEUED)
    await emitter.agent_status(run, AgentStatus.RUNNING)
    await emitter.agent_status(run, AgentStatus.FAILED, failure_reason="timeout")
    await emitter.agent_status(run, AgentStatus.RETRYING)
    await emitter.agent_status(run, AgentStatus.RUNNING)
    await emitter.agent_status(run, AgentStatus.COMPLETED, output_payload={"ok": True})

    assert run.failure_reason is None
    assert run.output_payload == {"ok": True}
    assert run.completed_at is not None


@pytest.mark.asyncio
async def test_job_status_publishes_to_both_channels(
    session: AsyncSession, publisher: InMemoryEventPublisher, queued_job: Job
) -> None:
    """The Job Queue View listens globally rather than per-job."""
    emitter = EventEmitter(session, publisher, queued_job.id)

    await emitter.job_status(queued_job, JobStatus.RUNNING)

    channels = [channel for channel, _ in publisher.messages]
    assert channels == [
        job_events_channel(str(queued_job.id)),
        GLOBAL_EVENTS_CHANNEL,
    ]


@pytest.mark.asyncio
async def test_illegal_transition_raises_and_does_not_publish(
    session: AsyncSession, publisher: InMemoryEventPublisher, queued_job: Job
) -> None:
    run = await _make_run(session, queued_job)
    emitter = EventEmitter(session, publisher, queued_job.id)

    with pytest.raises(IllegalTransitionError):
        await emitter.agent_status(run, AgentStatus.COMPLETED)

    assert publisher.messages == [], "an illegal transition must emit nothing"
    assert run.status is AgentStatus.IDLE, "and must not mutate the row"


@pytest.mark.asyncio
async def test_publish_failure_does_not_lose_persisted_state(
    session: AsyncSession, queued_job: Job
) -> None:
    """A Redis outage costs liveness, not data - reconnect backfills from Postgres."""

    class BrokenPublisher(InMemoryEventPublisher):
        async def publish(self, channel: str, message: str) -> None:
            raise ConnectionError("redis is down")

    emitter = EventEmitter(session, BrokenPublisher(), queued_job.id)
    await emitter.log("this still has to be recorded")

    count = await session.scalar(select(func.count()).select_from(AgentLog))
    assert count == 1


@pytest.mark.asyncio
async def test_job_duration_is_computed_from_timestamps(
    session: AsyncSession, publisher: InMemoryEventPublisher, queued_job: Job
) -> None:
    emitter = EventEmitter(session, publisher, queued_job.id)
    assert queued_job.duration_ms is None, "no duration before it starts"

    await emitter.job_status(queued_job, JobStatus.RUNNING)
    assert queued_job.duration_ms is None, "no duration while still running"

    await emitter.job_status(queued_job, JobStatus.COMPLETED, final_output="done")
    assert queued_job.duration_ms is not None and queued_job.duration_ms >= 0


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_fails_jobs_orphaned_by_a_crashed_worker(
    session: AsyncSession, publisher: InMemoryEventPublisher
) -> None:
    """Without this, a killed worker leaves a job spinning in the UI forever."""
    job = Job(
        id=uuid.uuid4(),
        task_description="orphaned run",
        status=JobStatus.RUNNING,
    )
    session.add(job)
    await session.commit()

    run = AgentRun(
        job_id=job.id,
        agent_name="Worker-1",
        sequence_index=2,
        status=AgentStatus.RUNNING,
        attempt_count=1,
        rework_count=0,
        input_payload={},
    )
    session.add(run)
    await session.commit()

    reconciled = await reconcile_orphaned_jobs(session, publisher)

    assert reconciled == 1
    assert job.status is JobStatus.FAILED
    assert job.failure_reason is not None
    assert run.status is AgentStatus.FAILED

    levels = [
        json.loads(raw)["payload"].get("level")
        for _, raw in publisher.messages
        if json.loads(raw)["event_type"] == "log_line"
    ]
    assert LogLevel.ERROR.value in levels


@pytest.mark.asyncio
async def test_reconcile_ignores_finished_jobs(
    session: AsyncSession, publisher: InMemoryEventPublisher, queued_job: Job
) -> None:
    assert await reconcile_orphaned_jobs(session, publisher) == 0
    assert queued_job.status is JobStatus.QUEUED
