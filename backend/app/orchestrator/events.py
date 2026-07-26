"""The single choke point for all state transitions and log lines.

TECH_SPEC 8.4: every transition must (1) persist to Postgres and (2) publish to
Redis Pub/Sub. Nothing else in the codebase may mutate a status column or call
``redis.publish`` directly.

Ordering matters: **commit the database write, then publish.** Publishing first
would let the frontend render a state that a rolled-back transaction never
persisted, breaking the "event bus is the source of truth" guarantee - and a
client that reconnects backfills from Postgres, so an unpersisted event is
permanently lost.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis_client import (
    GLOBAL_EVENTS_CHANNEL,
    job_events_channel,
)
from app.models.db_models import AgentLog, AgentRun, AgentStatus, Job, JobStatus, LogLevel
from app.models.schemas import (
    AgentStatusChangedEvent,
    AgentStatusChangedPayload,
    JobStatusChangedEvent,
    JobStatusChangedPayload,
    LogLineEvent,
    LogLinePayload,
)

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Timezone-aware UTC now. Never ``datetime.utcnow()`` (naive, deprecated)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Legal agent transitions (TECH_SPEC 3.1)
#
# Enforced rather than assumed: an illegal transition is a bug in the pipeline
# and should surface as one, not as a confusing UI state.
# ---------------------------------------------------------------------------

LEGAL_AGENT_TRANSITIONS: dict[AgentStatus, frozenset[AgentStatus]] = {
    AgentStatus.IDLE: frozenset({AgentStatus.QUEUED}),
    AgentStatus.QUEUED: frozenset({AgentStatus.RUNNING}),
    AgentStatus.RUNNING: frozenset({AgentStatus.COMPLETED, AgentStatus.FAILED}),
    # A failed agent either retries or stays failed (retries exhausted). It may
    # also be re-queued by a Verifier rework cycle.
    AgentStatus.FAILED: frozenset({AgentStatus.RETRYING, AgentStatus.QUEUED}),
    AgentStatus.RETRYING: frozenset({AgentStatus.RUNNING}),
    # Terminal. A completed agent can be re-queued only for Verifier rework.
    AgentStatus.COMPLETED: frozenset({AgentStatus.QUEUED}),
}

LEGAL_JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.FAILED}),
    JobStatus.RUNNING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
}


class IllegalTransitionError(RuntimeError):
    def __init__(self, kind: str, previous: object, new: object) -> None:
        super().__init__(
            f"illegal {kind} transition: {getattr(previous, 'value', previous)} "
            f"-> {getattr(new, 'value', new)}"
        )
        self.previous = previous
        self.new = new


def assert_legal_agent_transition(
    previous: AgentStatus, new: AgentStatus
) -> None:
    if new not in LEGAL_AGENT_TRANSITIONS.get(previous, frozenset()):
        raise IllegalTransitionError("agent status", previous, new)


def assert_legal_job_transition(previous: JobStatus, new: JobStatus) -> None:
    if new not in LEGAL_JOB_TRANSITIONS.get(previous, frozenset()):
        raise IllegalTransitionError("job status", previous, new)


# ---------------------------------------------------------------------------
# Publisher indirection
#
# The orchestrator depends on this narrow interface, not on Redis, so unit tests
# can capture emitted events without a live Redis.
# ---------------------------------------------------------------------------


class EventPublisher:
    """Publishes a serialized event to a channel."""

    async def publish(self, channel: str, message: str) -> None:  # pragma: no cover
        raise NotImplementedError


class RedisEventPublisher(EventPublisher):
    def __init__(self, client) -> None:  # redis.asyncio.Redis
        self._client = client

    async def publish(self, channel: str, message: str) -> None:
        await self._client.publish(channel, message)


class InMemoryEventPublisher(EventPublisher):
    """Test double. Records every (channel, message) pair."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> None:
        self.messages.append((channel, message))


# ---------------------------------------------------------------------------
# The emitter
# ---------------------------------------------------------------------------


class EventEmitter:
    """Persist-then-publish for every observable change in a run."""

    def __init__(
        self,
        session: AsyncSession,
        publisher: EventPublisher,
        job_id: uuid.UUID,
    ) -> None:
        self._session = session
        self._publisher = publisher
        self._job_id = job_id

    # -- internals ---------------------------------------------------------

    async def _publish(self, event, *, also_global: bool = False) -> None:
        # model_dump_json() (not json.dumps(model_dump())) - it serializes the
        # UUID and datetime fields, which plain model_dump() leaves as Python
        # objects that json.dumps then rejects.
        message = event.model_dump_json()
        job_key = str(self._job_id)
        try:
            await self._publisher.publish(job_events_channel(job_key), message)
            if also_global:
                await self._publisher.publish(GLOBAL_EVENTS_CHANNEL, message)
        except Exception:
            # A pub/sub outage must not abort an in-flight pipeline. The write
            # is already committed, so a reconnecting client backfills from
            # Postgres and loses nothing but liveness.
            logger.exception(
                "failed to publish event for job %s; state is persisted", job_key
            )

    # -- log lines ---------------------------------------------------------

    async def log(
        self,
        message: str,
        *,
        level: LogLevel = LogLevel.INFO,
        agent_name: str | None = None,
        agent_run_id: uuid.UUID | None = None,
    ) -> AgentLog:
        """Persist a log row, then stream it."""
        entry = AgentLog(
            job_id=self._job_id,
            agent_run_id=agent_run_id,
            agent_name=agent_name,
            timestamp=utcnow(),
            level=level,
            message=message,
        )
        self._session.add(entry)
        await self._session.commit()

        await self._publish(
            LogLineEvent(
                job_id=self._job_id,
                timestamp=entry.timestamp,
                payload=LogLinePayload(
                    log_id=entry.id,
                    agent_name=agent_name,
                    level=level,
                    message=message,
                ),
            )
        )
        return entry

    # -- agent status ------------------------------------------------------

    async def agent_status(
        self,
        run: AgentRun,
        new_status: AgentStatus,
        *,
        failure_reason: str | None = None,
        output_payload: dict | None = None,
    ) -> AgentRun:
        """Transition an agent run, persist it, then stream the change."""
        previous = run.status
        assert_legal_agent_transition(previous, new_status)

        run.status = new_status
        now = utcnow()

        if new_status is AgentStatus.RUNNING:
            run.attempt_count += 1
            if run.started_at is None:
                run.started_at = now
            # A rework cycle re-runs a completed agent; clear the stale end time
            # so duration reflects the current attempt.
            run.completed_at = None
        elif new_status in (AgentStatus.COMPLETED, AgentStatus.FAILED):
            run.completed_at = now

        if failure_reason is not None:
            run.failure_reason = failure_reason
        if new_status is AgentStatus.COMPLETED:
            run.failure_reason = None
        if output_payload is not None:
            run.output_payload = output_payload

        await self._session.commit()

        await self._publish(
            AgentStatusChangedEvent(
                job_id=self._job_id,
                timestamp=now,
                payload=AgentStatusChangedPayload(
                    agent_run_id=run.id,
                    agent_name=run.agent_name,
                    sequence_index=run.sequence_index,
                    previous_status=previous,
                    new_status=new_status,
                    attempt_count=run.attempt_count,
                    rework_count=run.rework_count,
                    failure_reason=run.failure_reason,
                ),
            )
        )
        return run

    # -- job status --------------------------------------------------------

    async def job_status(
        self,
        job: Job,
        new_status: JobStatus,
        *,
        final_output: str | None = None,
        failure_reason: str | None = None,
    ) -> Job:
        """Transition the job, persist it, then stream to both channels.

        Job-level changes also go to the global channel so the Job Queue View
        updates without subscribing to every job individually.
        """
        previous = job.status
        assert_legal_job_transition(previous, new_status)

        job.status = new_status
        now = utcnow()

        if new_status is JobStatus.RUNNING and job.started_at is None:
            job.started_at = now
        elif new_status in (JobStatus.COMPLETED, JobStatus.FAILED):
            job.completed_at = now

        if final_output is not None:
            job.final_output = final_output
        if failure_reason is not None:
            job.failure_reason = failure_reason

        await self._session.commit()

        await self._publish(
            JobStatusChangedEvent(
                job_id=self._job_id,
                timestamp=now,
                payload=JobStatusChangedPayload(
                    previous_status=previous,
                    new_status=new_status,
                    task_description=job.task_description,
                    failure_reason=job.failure_reason,
                    duration_ms=job.duration_ms,
                ),
            ),
            also_global=True,
        )
        return job


async def reconcile_orphaned_jobs(
    session: AsyncSession, publisher: EventPublisher
) -> int:
    """Fail jobs left ``running`` by a crashed worker.

    Run at worker startup. Without this, a mid-run crash leaves a job spinning
    in the UI forever with no agent to advance it.
    """
    result = await session.execute(select(Job).where(Job.status == JobStatus.RUNNING))
    orphans = list(result.scalars())

    for job in orphans:
        emitter = EventEmitter(session, publisher, job.id)
        runs = await session.execute(
            select(AgentRun).where(
                AgentRun.job_id == job.id,
                AgentRun.status.in_(
                    [AgentStatus.RUNNING, AgentStatus.QUEUED, AgentStatus.RETRYING]
                ),
            )
        )
        for run in runs.scalars():
            if run.status is not AgentStatus.RUNNING:
                # QUEUED/RETRYING must pass through RUNNING to reach FAILED.
                await emitter.agent_status(run, AgentStatus.RUNNING)
            await emitter.agent_status(
                run,
                AgentStatus.FAILED,
                failure_reason="worker process terminated mid-run",
            )

        await emitter.log(
            "Run marked failed during startup reconciliation: the worker "
            "process terminated while this job was in flight.",
            level=LogLevel.ERROR,
        )
        await emitter.job_status(
            job,
            JobStatus.FAILED,
            failure_reason="worker process terminated mid-run",
        )

    if orphans:
        logger.warning("reconciled %d orphaned running job(s)", len(orphans))
    return len(orphans)
