"""SQLAlchemy 2.0 models mirroring the schema in TECH_SPEC 4.1 / 4.2.

Alembic's ``target_metadata`` is ``Base.metadata`` from this module, so every
model must be defined (or imported) here or autogenerate will miss it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    JSON,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Cross-dialect column types
#
# Postgres is the only production target (TECH_SPEC 2), and the migration in
# alembic/versions/ emits native UUID and JSONB. The SQLite variants exist so
# unit tests can run against an in-memory database without a live Postgres -
# `.with_variant` keeps Postgres as the default and swaps only for SQLite.
# ---------------------------------------------------------------------------

UUID_TYPE = PgUUID(as_uuid=True).with_variant(Uuid(as_uuid=True), "sqlite")
JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


# ---------------------------------------------------------------------------
# Enums
#
# `values_callable` is required: without it SQLAlchemy persists the member
# *names* ("QUEUED") instead of the values ("queued"), which would break the
# lowercase API contract in TECH_SPEC 5.
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentStatus(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


class LogLevel(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


def _pg_enum(enum_cls: type[Enum], name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda e: [member.value for member in e],
    )


JOB_STATUS_ENUM = _pg_enum(JobStatus, "job_status")
AGENT_STATUS_ENUM = _pg_enum(AgentStatus, "agent_status")
LOG_LEVEL_ENUM = _pg_enum(LogLevel, "log_level")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        JOB_STATUS_ENUM, nullable=False, default=JobStatus.QUEUED
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_output: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    # lazy="raise" makes an accidental lazy load fail loudly at development
    # time instead of raising MissingGreenlet from inside a request.
    agent_runs: Mapped[list["AgentRun"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="AgentRun.sequence_index",
        lazy="raise",
    )
    logs: Mapped[list["AgentLog"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="AgentLog.timestamp",
        lazy="raise",
    )

    __table_args__ = (
        # Job queue list view: filter by status, sort by recency.
        Index("ix_jobs_status_created_at", "status", text("created_at DESC")),
    )

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None or self.completed_at is None:
            return None
        return int((self.completed_at - self.started_at).total_seconds() * 1000)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    sequence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AgentStatus] = mapped_column(
        AGENT_STATUS_ENUM, nullable=False, default=AgentStatus.IDLE
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Verifier rejections are tracked separately from technical retries so a
    # rework cycle never consumes the technical retry budget (TECH_SPEC 3.2).
    rework_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    output_payload: Mapped[dict | None] = mapped_column(JSON_TYPE)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    job: Mapped[Job] = relationship(back_populates="agent_runs", lazy="raise")
    logs: Mapped[list["AgentLog"]] = relationship(
        back_populates="agent_run", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        # Ordered pipeline reconstruction for the run detail view.
        Index("ix_agent_runs_job_id_sequence", "job_id", "sequence_index"),
    )

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None or self.completed_at is None:
            return None
        return int((self.completed_at - self.started_at).total_seconds() * 1000)


class AgentLog(Base):
    __tablename__ = "agent_logs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    # Null for job-level (non-agent-scoped) log lines.
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    # Denormalised so the log panel can label and filter rows without joining.
    agent_name: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    level: Mapped[LogLevel] = mapped_column(
        LOG_LEVEL_ENUM, nullable=False, default=LogLevel.INFO
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)

    job: Mapped[Job] = relationship(back_populates="logs", lazy="raise")
    agent_run: Mapped[AgentRun | None] = relationship(
        back_populates="logs", lazy="raise"
    )

    __table_args__ = (
        # Chronological retrieval and `?since=` cursor pagination.
        Index("ix_agent_logs_job_id_timestamp", "job_id", "timestamp"),
    )
