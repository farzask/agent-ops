"""FastAPI application: lifespan wiring, CORS, routers, health."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import jobs, websockets
from app.core.config import get_settings
from app.core.db import dispose_engine, get_session_factory, init_engine
from app.core.redis_client import close_redis, get_redis, init_redis
from app.models.schemas import HealthResponse

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # Clients are created once per process, never per request - a per-request
    # client leaks connection pools.
    init_engine()
    init_redis()
    logger.info("AgentOps API started (llm_provider=%s)", settings.llm_provider)
    try:
        yield
    finally:
        await close_redis()
        await dispose_engine()
        logger.info("AgentOps API stopped")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="AgentOps",
        description=(
            "Multi-agent pipeline observability. Orchestration is hand-written "
            "over raw LLM HTTP calls - no agent framework."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(jobs.router, prefix=API_PREFIX)
    # WebSocket routes are not under /api/v1 - TECH_SPEC 6 specifies /ws/jobs.
    app.include_router(websockets.router)

    @app.get("/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        """Liveness plus real datastore connectivity (TECH_SPEC 5).

        Runs an actual query and PING - reporting healthy because a client object
        exists would defeat the purpose.
        """
        problems: list[str] = []

        postgres_ok = False
        try:
            async with get_session_factory()() as session:
                await session.execute(text("SELECT 1"))
            postgres_ok = True
        except Exception as exc:
            problems.append(f"postgres: {type(exc).__name__}: {exc}")

        redis_ok = False
        try:
            redis_ok = bool(await get_redis().ping())
        except Exception as exc:
            problems.append(f"redis: {type(exc).__name__}: {exc}")

        healthy = postgres_ok and redis_ok
        return HealthResponse(
            status="ok" if healthy else "degraded",
            postgres=postgres_ok,
            redis=redis_ok,
            detail="; ".join(problems) or None,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        # Log the detail, return a generic body. Never leak a traceback or a
        # connection string to a client.
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500, content={"detail": "internal server error"}
        )

    return app


app = create_app()
