"""Pydantic v2 schemas.

REST shapes come from TECH_SPEC 5; the WebSocket envelope and payloads from
TECH_SPEC 6. The TypeScript mirror of the event types lives in
``frontend/src/lib/events.ts`` - change both in the same commit.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.db_models import AgentStatus, JobStatus, LogLevel

# ---------------------------------------------------------------------------
# REST - requests
# ---------------------------------------------------------------------------


class JobCreateRequest(BaseModel):
    task_description: str = Field(min_length=1, max_length=4000)


# ---------------------------------------------------------------------------
# REST - responses
# ---------------------------------------------------------------------------


class JobCreateResponse(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    created_at: datetime


class JobSummary(BaseModel):
    """One row of the Job Queue View."""

    job_id: uuid.UUID
    task_description: str
    status: JobStatus
    created_at: datetime
    duration_ms: int | None = None


class JobListResponse(BaseModel):
    jobs: list[JobSummary]
    total: int


class AgentRunDetail(BaseModel):
    agent_run_id: uuid.UUID
    agent_name: str
    sequence_index: int
    status: AgentStatus
    attempt_count: int
    rework_count: int
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    output_payload: dict | None = None
    failure_reason: str | None = None


class JobDetailResponse(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    task_description: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    final_output: str | None = None
    failure_reason: str | None = None
    agent_runs: list[AgentRunDetail]


class LogEntry(BaseModel):
    log_id: uuid.UUID
    agent_name: str | None = None
    timestamp: datetime
    level: LogLevel
    message: str


class LogListResponse(BaseModel):
    logs: list[LogEntry]
    # Cursor to pass back as `?since=` on the next poll. Null when empty.
    next_since: datetime | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    postgres: bool
    redis: bool
    detail: str | None = None


# ---------------------------------------------------------------------------
# WebSocket payloads (TECH_SPEC 6)
# ---------------------------------------------------------------------------


class AgentStatusChangedPayload(BaseModel):
    agent_run_id: uuid.UUID | None = None
    agent_name: str
    sequence_index: int
    previous_status: AgentStatus | None = None
    new_status: AgentStatus
    attempt_count: int = 0
    rework_count: int = 0
    failure_reason: str | None = None


class LogLinePayload(BaseModel):
    log_id: uuid.UUID | None = None
    agent_name: str | None = None
    level: LogLevel = LogLevel.INFO
    message: str


class JobStatusChangedPayload(BaseModel):
    previous_status: JobStatus | None = None
    new_status: JobStatus
    task_description: str | None = None
    failure_reason: str | None = None
    duration_ms: int | None = None


# ---------------------------------------------------------------------------
# WebSocket envelope
#
# A discriminated union on `event_type`, so the frontend's exhaustive switch and
# this model stay in lockstep.
# ---------------------------------------------------------------------------


class _Envelope(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    job_id: uuid.UUID
    timestamp: datetime


class AgentStatusChangedEvent(_Envelope):
    event_type: Literal["agent_status_changed"] = "agent_status_changed"
    payload: AgentStatusChangedPayload


class LogLineEvent(_Envelope):
    event_type: Literal["log_line"] = "log_line"
    payload: LogLinePayload


class JobStatusChangedEvent(_Envelope):
    event_type: Literal["job_status_changed"] = "job_status_changed"
    payload: JobStatusChangedPayload


Event = Annotated[
    AgentStatusChangedEvent | LogLineEvent | JobStatusChangedEvent,
    Field(discriminator="event_type"),
]
