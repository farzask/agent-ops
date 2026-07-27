"""WebSocket handlers.

TECH_SPEC 6. These processes do not run the orchestrator - they subscribe to
Redis Pub/Sub and forward. That indirection is what lets the API scale
horizontally without any instance missing events.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.redis_client import (
    GLOBAL_EVENTS_CHANNEL,
    get_redis,
    job_events_channel,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websockets"])

# A client that stops reading must never apply backpressure to the orchestrator.
# Past this many buffered messages we drop the connection; the frontend
# reconnects and backfills from Postgres, which is the designed recovery path.
MAX_CLIENT_BUFFER = 500

# How long to block waiting for a pub/sub message before looping. Bounded so a
# cancelled task actually stops rather than hanging on a dead subscription.
PUBSUB_POLL_TIMEOUT = 1.0


async def _pump(websocket: WebSocket, pubsub, label: str) -> None:
    """Forward messages from ``pubsub`` to ``websocket`` until disconnect."""
    outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=MAX_CLIENT_BUFFER)

    async def reader() -> None:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=PUBSUB_POLL_TIMEOUT
            )
            if message is None:
                continue
            data = message.get("data")
            if not isinstance(data, str):
                continue
            try:
                outbox.put_nowait(data)
            except asyncio.QueueFull:
                logger.warning(
                    "%s: client buffer full (%d), dropping connection",
                    label,
                    MAX_CLIENT_BUFFER,
                )
                raise

    async def writer() -> None:
        while True:
            data = await outbox.get()
            await websocket.send_text(data)

    async def receiver() -> None:
        # We never expect client messages, but we must keep reading so a browser
        # close frame surfaces as WebSocketDisconnect instead of hanging.
        while True:
            await websocket.receive_text()

    tasks = [
        asyncio.create_task(reader(), name=f"{label}-reader"),
        asyncio.create_task(writer(), name=f"{label}-writer"),
        asyncio.create_task(receiver(), name=f"{label}-receiver"),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in pending:
            task.cancel()
        # Surface the first real error (WebSocketDisconnect included) so the
        # caller's handler logs it at the right level.
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/ws/jobs/{job_id}")
async def job_events(websocket: WebSocket, job_id: uuid.UUID) -> None:
    """Live events for one job."""
    await websocket.accept()
    channel = job_events_channel(str(job_id))
    pubsub = get_redis().pubsub()

    try:
        # Subscribe before the client does any backfill fetch, so nothing is
        # lost in the window between reading state and starting to listen.
        await pubsub.subscribe(channel)
        logger.debug("ws subscribed to %s", channel)
        await _pump(websocket, pubsub, label=f"ws:{job_id}")
    except WebSocketDisconnect:
        logger.debug("ws client disconnected from %s", channel)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("ws error on %s", channel)
    finally:
        try:
            await pubsub.unsubscribe(channel)
        finally:
            await pubsub.aclose()


@router.websocket("/ws/jobs")
async def all_job_events(websocket: WebSocket) -> None:
    """Lightweight job-level status feed for the Job Queue View."""
    await websocket.accept()
    pubsub = get_redis().pubsub()

    try:
        await pubsub.subscribe(GLOBAL_EVENTS_CHANNEL)
        logger.debug("ws subscribed to %s", GLOBAL_EVENTS_CHANNEL)
        await _pump(websocket, pubsub, label="ws:global")
    except WebSocketDisconnect:
        logger.debug("ws client disconnected from %s", GLOBAL_EVENTS_CHANNEL)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("ws error on %s", GLOBAL_EVENTS_CHANNEL)
    finally:
        try:
            await pubsub.unsubscribe(GLOBAL_EVENTS_CHANNEL)
        finally:
            await pubsub.aclose()
