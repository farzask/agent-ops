"""REST endpoint contracts (TECH_SPEC 5).

The app is driven through httpx's ASGI transport - no network, no live server.
Postgres is swapped for in-memory SQLite and Redis for a fake, via dependency
overrides.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import get_session
from app.main import create_app
from app.models.db_models import AgentLog, AgentRun, AgentStatus, Job, JobStatus, LogLevel
from app.orchestrator.events import utcnow


class FakeRedis:
    """Just enough Redis for the endpoints under test."""

    def __init__(self, *, fail_on_push: bool = False) -> None:
        self.pushed: list[str] = []
        self.fail_on_push = fail_on_push

    async def lpush(self, key: str, value: str) -> int:
        if self.fail_on_push:
            raise ConnectionError("redis is down")
        self.pushed.append(value)
        return len(self.pushed)

    async def ping(self) -> bool:
        return True


@pytest_asyncio.fixture
async def api(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncClient, FakeRedis]]:
    fake_redis = FakeRedis()
    monkeypatch.setattr("app.api.jobs.get_redis", lambda: fake_redis)

    app = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override_session

    # Bypass the lifespan: it would try to reach the real Postgres and Redis.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, fake_redis


# ---------------------------------------------------------------------------
# POST /jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_job_returns_201_and_enqueues(api) -> None:
    client, fake_redis = api

    response = await client.post(
        "/api/v1/jobs",
        json={"task_description": "Write a 500-word blog post about leak detection"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "queued"
    uuid.UUID(body["job_id"])  # must be a real UUID
    assert body["created_at"]

    assert len(fake_redis.pushed) == 1
    assert body["job_id"] in fake_redis.pushed[0]


@pytest.mark.asyncio
async def test_create_job_rejects_empty_description(api) -> None:
    client, _ = api
    response = await client.post("/api/v1/jobs", json={"task_description": ""})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_job_rejects_missing_field(api) -> None:
    client, _ = api
    assert (await client.post("/api/v1/jobs", json={})).status_code == 422


@pytest.mark.asyncio
async def test_enqueue_failure_marks_job_failed_not_stuck_queued(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job that can never be consumed must not sit `queued` forever."""
    monkeypatch.setattr("app.api.jobs.get_redis", lambda: FakeRedis(fail_on_push=True))
    app = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/jobs", json={"task_description": "will not enqueue"})
        assert response.status_code == 503

        listing = (await client.get("/api/v1/jobs")).json()

    assert listing["total"] == 1
    assert listing["jobs"][0]["status"] == "failed"


# ---------------------------------------------------------------------------
# GET /jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_jobs_is_newest_first_with_real_total(api) -> None:
    client, _ = api
    for i in range(5):
        await client.post("/api/v1/jobs", json={"task_description": f"task {i}"})

    response = await client.get("/api/v1/jobs", params={"limit": 2})
    body = response.json()

    assert response.status_code == 200
    assert len(body["jobs"]) == 2
    # `total` is a COUNT of all matching rows, not the page length.
    assert body["total"] == 5
    assert body["jobs"][0]["task_description"] == "task 4"


@pytest.mark.asyncio
async def test_list_jobs_filters_by_status(
    api, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    client, _ = api
    await client.post("/api/v1/jobs", json={"task_description": "stays queued"})
    created = await client.post("/api/v1/jobs", json={"task_description": "completes"})
    job_id = uuid.UUID(created.json()["job_id"])

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.status = JobStatus.COMPLETED
        job.started_at = utcnow()
        job.completed_at = utcnow()
        await session.commit()

    completed = (await client.get("/api/v1/jobs", params={"status": "completed"})).json()
    assert completed["total"] == 1
    assert completed["jobs"][0]["task_description"] == "completes"
    assert completed["jobs"][0]["duration_ms"] is not None

    queued = (await client.get("/api/v1/jobs", params={"status": "queued"})).json()
    assert queued["total"] == 1


@pytest.mark.asyncio
async def test_list_jobs_rejects_unknown_status(api) -> None:
    client, _ = api
    response = await client.get("/api/v1/jobs", params={"status": "banana"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_list_jobs_paginates_with_offset(api) -> None:
    client, _ = api
    for i in range(4):
        await client.post("/api/v1/jobs", json={"task_description": f"task {i}"})

    page2 = (await client.get("/api/v1/jobs", params={"limit": 2, "offset": 2})).json()
    assert [j["task_description"] for j in page2["jobs"]] == ["task 1", "task 0"]
    assert page2["total"] == 4


# ---------------------------------------------------------------------------
# GET /jobs/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_job_returns_agent_runs_in_sequence_order(
    api, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    client, _ = api
    created = await client.post("/api/v1/jobs", json={"task_description": "detail"})
    job_id = uuid.UUID(created.json()["job_id"])

    async with session_factory() as session:
        # Inserted out of order on purpose - the endpoint must sort.
        for name, index in [("Verifier", 3), ("Supervisor", 0), ("Clarifier", 1)]:
            session.add(
                AgentRun(
                    job_id=job_id,
                    agent_name=name,
                    sequence_index=index,
                    status=AgentStatus.COMPLETED,
                    attempt_count=1,
                    rework_count=0,
                    input_payload={},
                    output_payload={"ok": True},
                )
            )
        await session.commit()

    body = (await client.get(f"/api/v1/jobs/{job_id}")).json()

    assert [r["agent_name"] for r in body["agent_runs"]] == [
        "Supervisor",
        "Clarifier",
        "Verifier",
    ]
    assert body["agent_runs"][0]["output_payload"] == {"ok": True}
    assert body["task_description"] == "detail"


@pytest.mark.asyncio
async def test_get_job_404_for_unknown_id(api) -> None:
    client, _ = api
    response = await client.get(f"/api/v1/jobs/{uuid.uuid4()}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_job_422_for_malformed_id(api) -> None:
    client, _ = api
    assert (await client.get("/api/v1/jobs/not-a-uuid")).status_code == 422


# ---------------------------------------------------------------------------
# GET /jobs/{id}/logs - the reconnect backfill path (TECH_SPEC 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logs_since_cursor_excludes_already_seen_rows(
    api, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    client, _ = api
    created = await client.post("/api/v1/jobs", json={"task_description": "logs"})
    job_id = uuid.UUID(created.json()["job_id"])

    async with session_factory() as session:
        for i in range(5):
            session.add(
                AgentLog(
                    job_id=job_id,
                    agent_name="Supervisor",
                    timestamp=utcnow(),
                    level=LogLevel.INFO,
                    message=f"line {i}",
                )
            )
            await session.commit()

    everything = (await client.get(f"/api/v1/jobs/{job_id}/logs")).json()
    assert [entry["message"] for entry in everything["logs"]] == [f"line {i}" for i in range(5)]
    assert everything["next_since"] is not None

    # Simulate a reconnect: ask for everything after the 2nd line.
    cursor = everything["logs"][1]["timestamp"]
    tail = (await client.get(f"/api/v1/jobs/{job_id}/logs", params={"since": cursor})).json()

    messages = [entry["message"] for entry in tail["logs"]]
    assert "line 1" not in messages, "the cursor row must not be re-sent"
    assert messages == ["line 2", "line 3", "line 4"]


@pytest.mark.asyncio
async def test_logs_returns_cursor_unchanged_when_nothing_new(
    api, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    client, _ = api
    created = await client.post("/api/v1/jobs", json={"task_description": "empty"})
    job_id = created.json()["job_id"]

    body = (await client.get(f"/api/v1/jobs/{job_id}/logs")).json()
    assert body["logs"] == []
    assert body["next_since"] is None


@pytest.mark.asyncio
async def test_logs_404_for_unknown_job(api) -> None:
    client, _ = api
    response = await client.get(f"/api/v1/jobs/{uuid.uuid4()}/logs")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_degraded_when_a_datastore_is_unreachable(
    api,
) -> None:
    """It must run a real query and PING, not just check that a client exists."""
    client, _ = api
    body = (await client.get("/health")).json()

    # The lifespan never ran, so the real engine and Redis are absent.
    assert body["status"] == "degraded"
    assert body["postgres"] is False
    assert body["detail"]
