---
name: sqlalchemy-async-db
description: SQLAlchemy 2.0 async + asyncpg conventions for the AgentOps Postgres layer — model declaration style, enum handling, UUID and timestamptz columns, the required indexes, eager-loading rules, and transaction boundaries. Use when adding or changing a table, column, index, or query under backend/app/models/ or any data access code.
---

# Async Postgres Layer (SQLAlchemy 2.0 + asyncpg)

Schema is specified in TECH_SPEC §4.1 and §4.2. The models mirror it exactly.

## Engine and sessions

```python
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
session_factory = async_sessionmaker(engine, expire_on_commit=False)
```

`expire_on_commit=False` is required. Without it, attribute access after
`commit()` triggers a lazy refresh, which raises `MissingGreenlet` under asyncio.
This is the single most common async SQLAlchemy bug — do not remove that flag.

URL scheme must be `postgresql+asyncpg://`. A bare `postgresql://` silently picks
the sync psycopg driver and fails at runtime.

## Model style — SQLAlchemy 2.0 typed declarative

Use `DeclarativeBase` + `Mapped` / `mapped_column`. Do not use the legacy
`declarative_base()` or bare `Column()` style.

```python
class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

## Column type rules

| Spec type | Use | Never use |
|---|---|---|
| UUID PK | `postgresql.UUID(as_uuid=True)`, default `uuid.uuid4` | `String(36)` |
| timestamptz | `DateTime(timezone=True)` | naive `DateTime` |
| jsonb | `postgresql.JSONB` | `JSON`, `Text` holding JSON |
| enum | see below | free-form `String` |
| text | `Text` | `String` without length |

All datetimes are timezone-aware UTC. `datetime.now(timezone.utc)`, never
`datetime.utcnow()` (which returns a naive value and is deprecated in 3.12+).

## Enums — Python enum + native Postgres type

Define a `str, Enum` in Python and map it to a **named** Postgres enum so Alembic
can manage it:

```python
class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

status: Mapped[JobStatus] = mapped_column(
    SAEnum(JobStatus, name="job_status", values_callable=lambda e: [m.value for m in e]),
)
```

`values_callable` is required. Without it SQLAlchemy stores the enum **member
names** (`QUEUED`) instead of the values (`queued`), which breaks the API
contract in TECH_SPEC §5 where lowercase values are specified.

Enum sets are fixed by spec:
- `job_status`: queued, running, completed, failed
- `agent_status`: idle, queued, running, completed, failed, retrying
- `log_level`: info, warn, error

## Required indexes

TECH_SPEC §4.2 — these are not optional, and each has a stated reason:

```python
Index("ix_jobs_status_created_at", Job.status, Job.created_at.desc())
Index("ix_agent_runs_job_id_sequence", AgentRun.job_id, AgentRun.sequence_index)
Index("ix_agent_logs_job_id_timestamp", AgentLog.job_id, AgentLog.timestamp)
```

## Relationships and eager loading

Lazy loading does not work under asyncio — touching an unloaded relationship
raises `MissingGreenlet`. Two acceptable approaches:

1. Declare `lazy="raise"` on relationships so the mistake fails loudly at
   development time rather than in production
2. Load explicitly with `selectinload()` in the query

`selectinload` is preferred over `joinedload` for collections — it avoids the
row multiplication that makes `LIMIT` behave wrongly on the job list endpoint.

Cascade: `agent_runs` and `agent_logs` are children of `jobs` with
`ondelete="CASCADE"` on the FK **and** `cascade="all, delete-orphan"` on the
relationship. `agent_logs.agent_run_id` is nullable (job-level logs).

## Queries — 2.0 style only

`select()` + `await session.execute()` + `.scalars()`. Never the legacy
`session.query()`. Never construct SQL by string interpolation; use bound
parameters or `text()` with params.

The job list endpoint paginates with `LIMIT`/`OFFSET` and returns a real `total`
from a separate `count()` — do not fake the total from the page length.

## Transaction boundaries

One logical operation, one transaction. Use `async with session.begin():` for
write paths so a failure rolls back cleanly. Do not sprinkle `commit()` calls
mid-operation; a partially committed pipeline step is unreconstructable state.

Because `emit_event()` writes to Postgres *and* publishes to Redis, order
matters: **commit the DB write first, then publish.** Publishing first risks the
frontend rendering a state that a rolled-back transaction never persisted, which
breaks the "event bus is the source of truth" guarantee.

## Health check

`GET /health` per TECH_SPEC §5 verifies real connectivity: `SELECT 1` against
Postgres and `PING` against Redis. It must not report healthy on the mere
existence of a client object.
