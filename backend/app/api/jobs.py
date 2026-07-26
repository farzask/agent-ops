"""REST endpoints. Contracts are fixed by TECH_SPEC 5."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.redis_client import get_redis
from app.models.db_models import AgentLog, AgentRun, Job, JobStatus
from app.models.schemas import (
    AgentRunDetail,
    JobCreateRequest,
    JobCreateResponse,
    JobDetailResponse,
    JobListResponse,
    JobSummary,
    LogEntry,
    LogListResponse,
)
from app.orchestrator.events import utcnow
from app.queue.job_queue import enqueue_job

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", response_model=JobCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: JobCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> JobCreateResponse:
    """Insert the job, then enqueue it.

    Order matters: the row is committed before the queue message is pushed, so a
    consumer can never pop an id that does not exist yet.
    """
    job = Job(
        task_description=payload.task_description.strip(),
        status=JobStatus.QUEUED,
        created_at=utcnow(),
    )
    session.add(job)
    await session.commit()

    try:
        await enqueue_job(get_redis(), job.id)
    except Exception as exc:
        # The row exists but nothing will ever run it. Fail it immediately
        # rather than leaving a job queued forever with no consumer.
        job.status = JobStatus.FAILED
        job.failure_reason = f"failed to enqueue job: {exc}"
        job.completed_at = utcnow()
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="job queue is unavailable; job was recorded as failed",
        ) from exc

    return JobCreateResponse(
        job_id=job.id, status=job.status, created_at=job.created_at
    )


@router.get("", response_model=JobListResponse)
async def list_jobs(
    session: AsyncSession = Depends(get_session),
    job_status: JobStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> JobListResponse:
    filters = [Job.status == job_status] if job_status is not None else []

    # A real COUNT, not the page length - the UI paginates on this.
    total = await session.scalar(
        select(func.count()).select_from(Job).where(*filters)
    )

    result = await session.execute(
        select(Job)
        .where(*filters)
        .order_by(Job.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    return JobListResponse(
        jobs=[
            JobSummary(
                job_id=job.id,
                task_description=job.task_description,
                status=job.status,
                created_at=job.created_at,
                duration_ms=job.duration_ms,
            )
            for job in result.scalars()
        ],
        total=int(total or 0),
    )


@router.get("/{job_id}", response_model=JobDetailResponse)
async def get_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> JobDetailResponse:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    # Explicit query rather than `job.agent_runs`: relationships are lazy="raise"
    # so an implicit load would fail loudly instead of silently blocking.
    runs = await session.execute(
        select(AgentRun)
        .where(AgentRun.job_id == job_id)
        .order_by(AgentRun.sequence_index)
    )

    return JobDetailResponse(
        job_id=job.id,
        status=job.status,
        task_description=job.task_description,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        duration_ms=job.duration_ms,
        final_output=job.final_output,
        failure_reason=job.failure_reason,
        agent_runs=[
            AgentRunDetail(
                agent_run_id=run.id,
                agent_name=run.agent_name,
                sequence_index=run.sequence_index,
                status=run.status,
                attempt_count=run.attempt_count,
                rework_count=run.rework_count,
                started_at=run.started_at,
                completed_at=run.completed_at,
                duration_ms=run.duration_ms,
                output_payload=run.output_payload,
                failure_reason=run.failure_reason,
            )
            for run in runs.scalars()
        ],
    )


@router.get("/{job_id}/logs", response_model=LogListResponse)
async def get_job_logs(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    since: datetime | None = Query(
        default=None,
        description="Return logs strictly after this timestamp. Used by the "
        "frontend to backfill after a WebSocket reconnect.",
    ),
    limit: int = Query(default=500, ge=1, le=2000),
) -> LogListResponse:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    filters = [AgentLog.job_id == job_id]
    if since is not None:
        # Strictly greater-than, so the caller does not re-receive the row it
        # already has as its cursor.
        filters.append(AgentLog.timestamp > since)

    result = await session.execute(
        select(AgentLog)
        .where(*filters)
        .order_by(AgentLog.timestamp, AgentLog.id)
        .limit(limit)
    )
    logs = [
        LogEntry(
            log_id=row.id,
            agent_name=row.agent_name,
            timestamp=row.timestamp,
            level=row.level,
            message=row.message,
        )
        for row in result.scalars()
    ]

    return LogListResponse(
        logs=logs,
        next_since=logs[-1].timestamp if logs else since,
    )
