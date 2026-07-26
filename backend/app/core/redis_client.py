"""Redis client and pub/sub channel naming.

Redis carries two things (TECH_SPEC 2): the job queue, and the pub/sub fan-out
that decouples the orchestrator process from the WebSocket-serving process.
"""

from __future__ import annotations

import redis.asyncio as redis

from app.core.config import get_settings

# Queue key. A Redis list consumed with a blocking pop (no poll-sleep loop).
JOB_QUEUE_KEY = "agentops:jobs:queue"

# Channel names are fixed - do not invent variants.
GLOBAL_EVENTS_CHANNEL = "jobs:events"

_client: redis.Redis | None = None


def job_events_channel(job_id: str) -> str:
    """Per-job event channel, e.g. ``job:<uuid>:events``."""
    return f"job:{job_id}:events"


def init_redis() -> redis.Redis:
    """Create the process-wide client. Called once from the app lifespan."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _client


def get_redis() -> redis.Redis:
    if _client is None:
        return init_redis()
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
