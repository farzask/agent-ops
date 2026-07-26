---
name: fastapi-backend
description: Conventions for the AgentOps FastAPI backend — async-everywhere rules, dependency injection, lifespan wiring, router layout, WebSocket handlers, the queue-consumer worker, and Pydantic v2 schema patterns. Use when adding or changing anything under backend/app/, adding an endpoint, or touching startup/shutdown wiring.
---

# FastAPI Backend Conventions

Module layout is fixed by TECH_SPEC §8.1. Do not reorganize it.

```
backend/app/
  main.py                 FastAPI app, lifespan, router mounting
  api/jobs.py             REST endpoints
  api/websockets.py       WS endpoint handlers
  core/config.py          pydantic-settings config
  core/db.py              async engine + session factory
  core/redis_client.py    redis.asyncio client + pubsub helpers
  orchestrator/*.py       agents, pipeline, llm_client, events
  models/db_models.py     SQLAlchemy 2.0 declarative models
  models/schemas.py       Pydantic v2 request/response/event schemas
  queue/job_queue.py      Redis-backed queue producer + consumer
worker.py                 entrypoint for the standalone queue consumer
```

## Async everywhere — no exceptions

Every I/O path is async. A single blocking call stalls the event loop and kills
the sub-500ms latency target in PRD §9.

- HTTP: `httpx.AsyncClient` (never `requests`, never `httpx.Client`)
- Postgres: `create_async_engine` + `asyncpg` (never `psycopg2`)
- Redis: `redis.asyncio` (never the sync `redis` client)
- Sleep: `asyncio.sleep` (never `time.sleep`) — this includes retry backoff

Route handlers are `async def`. If you genuinely need blocking work, wrap it in
`anyio.to_thread.run_sync`, and say why in a comment.

## Config

All config lives in `core/config.py` as a single `Settings` class using
`pydantic-settings`, read from env with a `.env` fallback. Access it via the
cached `get_settings()`. Never read `os.environ` anywhere else.

Every new setting needs: a typed field with a sane default, an entry in
`.env.example`, and a row in the README's config table.

## Lifespan, not startup events

Wire resources in an `asynccontextmanager` lifespan passed to `FastAPI(lifespan=...)`.
`@app.on_event("startup")` is deprecated — do not use it.

The lifespan creates and disposes: the SQLAlchemy engine, the Redis client, and
the shared `httpx.AsyncClient`. Clients are created **once** per process, not
per request. Creating an `AsyncClient` per call is a connection-pool leak.

## Dependency injection

Sessions come from a dependency that yields and always closes:

```python
async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
```

Handlers take `session: AsyncSession = Depends(get_session)`. Never construct a
session inside a handler body. Never share one session across concurrent tasks —
`AsyncSession` is not concurrency-safe.

## Routers

One router per file in `api/`, each with its own prefix and tags, all mounted in
`main.py` under the `/api/v1` prefix from TECH_SPEC §5. Response models are
declared explicitly (`response_model=...`) so the OpenAPI schema is accurate.

Status codes: `201` for job creation, `200` for reads, `404` for unknown job id,
`422` is FastAPI's automatic validation response — do not hand-roll it.

## WebSocket handlers

Pattern for `api/websockets.py`:

1. `await ws.accept()`
2. Subscribe to the Redis channel **before** any backfill read, so no event is
   lost in the gap between reading state and starting to listen
3. Forward messages in a loop
4. Always unsubscribe and close the pubsub in a `finally` block

Handle `WebSocketDisconnect` explicitly — an unhandled one spams the logs on
every browser navigation. A slow or dead client must never block the publisher:
forward with a bounded queue and drop the connection if it backs up, rather than
letting it apply backpressure to the orchestrator.

Do not send app state the client can't use; the envelope in
`agentops-conventions` Rule 3 is the whole wire format.

## The queue worker is a separate process

TECH_SPEC §8.2. `worker.py` runs the consumer loop; `uvicorn` runs the API. They
share code but not a process. This separation is a deliberate talking point —
never "simplify" it by running the orchestrator inside a request handler or a
`BackgroundTask`.

The consumer must:
- Use a blocking pop (`BRPOP`) rather than a poll-sleep loop
- Be cancellation-safe: on SIGTERM, finish or explicitly fail the in-flight job
- Never lose a job silently — a crash mid-run leaves the job `running`, so there
  is a startup reconciliation step that marks orphaned runs `failed`

## Pydantic v2

- `model_config = ConfigDict(...)`, not the v1 `class Config`
- `field_validator` / `model_validator`, not `validator` / `root_validator`
- `model_dump()` / `model_validate()`, not `dict()` / `parse_obj()`
- Use `model_dump(mode="json")` when the target is JSON (it serializes UUID and
  datetime correctly; plain `model_dump()` does not)
- Separate request, response, and DB schemas. Never return a SQLAlchemy model
  directly from a handler.

## Errors

Raise `HTTPException` for client-facing failures. Let unexpected exceptions hit
a single registered exception handler that logs with the job id and returns a
generic 500 — never leak a traceback or a connection string to the client.

## Testing

`pytest` + `pytest-asyncio` in strict mode + `httpx.ASGITransport` for the app.
Tests never touch a real LLM (the mock provider is the only one) and never
require a live Redis or Postgres for unit tests — fake those at the boundary.
Integration tests that do need them are marked and skipped when unavailable.
