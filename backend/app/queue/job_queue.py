"""Redis-backed job queue: producer side, and the consumer loop.

TECH_SPEC 8.2. The consumer runs in a **separate process** from the API
(``worker.py``), which is the point: request handling is decoupled from
long-running task execution. Do not "simplify" this by running the pipeline in a
request handler or a FastAPI BackgroundTask.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import uuid

import redis.asyncio as redis
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.core.config import Settings, get_settings
from app.core.redis_client import JOB_QUEUE_KEY
from app.orchestrator.events import RedisEventPublisher, reconcile_orphaned_jobs
from app.orchestrator.llm_client import LLMClient
from app.orchestrator.pipeline import Pipeline

logger = logging.getLogger(__name__)

# How long BRPOP blocks before returning empty, letting the loop check for
# shutdown. A blocking pop rather than a poll-sleep loop: no wasted round trips
# and no added latency on job pickup.
BRPOP_TIMEOUT_SECONDS = 5


async def enqueue_job(client: redis.Redis, job_id: uuid.UUID) -> None:
    """Producer. Called from ``POST /jobs`` after the row is committed."""
    await client.lpush(JOB_QUEUE_KEY, json.dumps({"job_id": str(job_id)}))


async def queue_depth(client: redis.Redis) -> int:
    return int(await client.llen(JOB_QUEUE_KEY))


class QueueWorker:
    """Consumes job ids and runs the pipeline for each, one at a time."""

    def __init__(
        self,
        redis_client: redis.Redis,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
    ) -> None:
        self._redis = redis_client
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._stopping = asyncio.Event()
        self._current_job: uuid.UUID | None = None

    def request_stop(self) -> None:
        """Signal a graceful stop. The in-flight job is allowed to finish."""
        if not self._stopping.is_set():
            logger.info("shutdown requested; finishing in-flight work")
            self._stopping.set()

    async def run_forever(self) -> None:
        publisher = RedisEventPublisher(self._redis)

        # Any job still `running` belongs to a previous process that died.
        # Without this, it spins in the UI forever with nothing to advance it.
        async with self._session_factory() as session:
            reconciled = await reconcile_orphaned_jobs(session, publisher)
        if reconciled:
            logger.warning("marked %d orphaned job(s) failed at startup", reconciled)

        llm_client = LLMClient(settings=self._settings)
        logger.info(
            "queue worker ready (provider=%s model=%s max_retries=%d)",
            self._settings.llm_provider,
            self._settings.llm_model,
            self._settings.max_retries,
        )

        try:
            while not self._stopping.is_set():
                job_id = await self._next_job()
                if job_id is None:
                    continue
                await self._process(job_id, llm_client, publisher)
        finally:
            await llm_client.aclose()
            logger.info("queue worker stopped")

    async def _next_job(self) -> uuid.UUID | None:
        try:
            popped = await self._redis.brpop(
                [JOB_QUEUE_KEY], timeout=BRPOP_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("error reading from queue; backing off 1s")
            await asyncio.sleep(1.0)
            return None

        if popped is None:
            return None  # timeout - loop back and re-check the stop flag

        _key, raw = popped
        try:
            payload = json.loads(raw)
            return uuid.UUID(payload["job_id"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            # Drop it rather than crash-looping on one bad message. It is gone
            # from the queue already, so log loudly.
            logger.error("discarding unparseable queue message: %r", raw)
            return None

    async def _process(
        self,
        job_id: uuid.UUID,
        llm_client: LLMClient,
        publisher: RedisEventPublisher,
    ) -> None:
        self._current_job = job_id
        logger.info("starting job %s", job_id)
        try:
            # One session per job: an AsyncSession is not concurrency-safe and
            # a long-lived shared one accumulates identity-map state.
            async with self._session_factory() as session:
                pipeline = Pipeline(session, publisher, llm_client, self._settings)
                result = await pipeline.run(job_id)
            logger.info("job %s finished: %s", job_id, result.status.value)
        except asyncio.CancelledError:
            logger.warning("job %s cancelled mid-run", job_id)
            raise
        except Exception:
            # Pipeline.run already converts its own failures into a persisted
            # `failed` status. Reaching here means the failure path itself broke,
            # which must not kill the worker.
            logger.exception("job %s raised past the pipeline error handler", job_id)
        finally:
            self._current_job = None


async def run_worker() -> None:
    """Entry point used by ``worker.py``."""
    from app.core.db import dispose_engine, get_session_factory, init_engine
    from app.core.redis_client import close_redis, init_redis

    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    init_engine()
    redis_client = init_redis()
    worker = QueueWorker(redis_client, get_session_factory(), settings)

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError):
            # add_signal_handler is not implemented on Windows' proactor loop;
            # there, KeyboardInterrupt handles SIGINT instead.
            loop.add_signal_handler(sig, worker.request_stop)

    try:
        await worker.run_forever()
    finally:
        await close_redis()
        await dispose_engine()
