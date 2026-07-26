"""Shared fixtures.

Unit tests run against in-memory SQLite via the dialect variants in
``db_models``. No test touches a real LLM (mock is the only provider) and no unit
test needs a live Redis - the publisher is swapped for an in-memory double.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.models.db_models import Base, Job, JobStatus
from app.orchestrator.events import InMemoryEventPublisher, utcnow


@pytest.fixture
def settings() -> Settings:
    """Fast, deterministic settings. Zero latency so tests don't sleep."""
    return Settings(
        database_url="postgresql+asyncpg://unused/unused",
        redis_url="redis://unused",
        llm_provider="mock",
        llm_model="mock-small",
        mock_latency_ms=0,
        mock_failure_rate=0.0,
        mock_malformed_rate=0.0,
        mock_seed=1234,
        max_retries=3,
        backoff_base_seconds=1.0,
        backoff_multiplier=3.0,
        max_rework_cycles=2,
    )


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # A shared in-memory SQLite database for the lifetime of one test.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest.fixture
def publisher() -> InMemoryEventPublisher:
    return InMemoryEventPublisher()


@pytest_asyncio.fixture
async def queued_job(session: AsyncSession) -> Job:
    job = Job(
        id=uuid.uuid4(),
        task_description="Write a 500-word blog post about IoT water leak detection",
        status=JobStatus.QUEUED,
        created_at=utcnow(),
    )
    session.add(job)
    await session.commit()
    return job


class RecordingSleep:
    """Captures backoff delays instead of waiting for them."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def recording_sleep() -> RecordingSleep:
    return RecordingSleep()
