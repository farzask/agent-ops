# AgentOps — Multi-Agent Pipeline Observability

A full-stack dashboard that shows a **hand-written** multi-agent LLM pipeline
executing in real time. Submit a task, watch the Supervisor decompose it, watch
Workers execute each subtask, watch the Verifier approve or send work back — with
live status transitions and streaming logs, all pushed over WebSockets.

Built against [`PRD.md`](PRD.md) and [`TECH_SPEC.md`](TECH_SPEC.md).

---

## The design decision that matters

**There is no agent framework in this codebase.** No LangChain, no LangGraph, no
CrewAI, no AutoGen, no LlamaIndex, no Pydantic AI — and no vendor SDK either.
LLM calls go over raw `httpx`.

Every piece a framework would hide is written by hand and is readable in one place:

| Concern | Where it lives | What a framework would have hidden |
|---|---|---|
| Prompt construction | [supervisor.py](backend/app/orchestrator/supervisor.py), [clarifier.py](backend/app/orchestrator/clarifier.py), [worker.py](backend/app/orchestrator/worker.py), [verifier.py](backend/app/orchestrator/verifier.py) | Prompt templates and the JSON-only discipline |
| Structured-output parsing | [llm_client.py](backend/app/orchestrator/llm_client.py) `extract_json` | "Structured output" / function-calling wrappers |
| Retry, backoff, error classification | [llm_client.py](backend/app/orchestrator/llm_client.py) `LLMClient` | Opaque retry decorators |
| Control flow between agents | [pipeline.py](backend/app/orchestrator/pipeline.py) | The agent loop / graph executor |
| State passed between agents | [base.py](backend/app/orchestrator/base.py) `PipelineContext` | Implicit shared memory |
| State machine enforcement | [events.py](backend/app/orchestrator/events.py) | Nothing — most frameworks don't enforce one |

This is enforced, not just asserted: CI fails the build if a banned package
appears in `requirements.txt` (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

---

## Architecture

```
┌──────────────────────┐   REST: submit job, fetch history   ┌────────────────────────┐
│   Next.js frontend   │ ──────────────────────────────────▶ │   FastAPI (uvicorn)    │
│                      │ ◀────────────────────────────────── │   REST + WebSocket     │
│  Dashboard           │   WebSocket: live status + logs      └───────────┬────────────┘
│  Pipeline diagram    │ ◀═══════════════════════════════════             │ subscribes
│  Log viewer          │                                                  │
└──────────────────────┘                                     ┌───────────▼────────────┐
                                                             │  Redis                 │
                                          ┌─────publishes───▶│  queue + pub/sub       │
                                          │                  └───────────┬────────────┘
                              ┌───────────┴──────────┐                   │ BRPOP
                              │  Worker process      │◀──────────────────┘
                              │  (queue consumer)    │
                              │  orchestrator        │        ┌────────────────────────┐
                              │  raw httpx LLM calls │───────▶│  Postgres              │
                              └──────────────────────┘        │  jobs, agent_runs,     │
                                                              │  agent_logs            │
                                                              └────────────────────────┘
```

Three architectural commitments, each with a reason:

**The event bus is the single source of truth.** Every state transition goes
through one function, [`emit_event`](backend/app/orchestrator/events.py), which
persists to Postgres *and then* publishes to Redis — in that order. The frontend
never polls and never infers state. Publishing before committing would let the UI
render a state a rolled-back transaction never persisted.

**The queue consumer is a separate process.** `worker.py` runs the orchestrator;
`uvicorn` serves requests. They share code, not a process. Long-running task
execution is decoupled from request handling, and the WebSocket-serving process
can scale horizontally without missing events, because it learns about them from
Redis rather than from local memory.

**Retries and failures are visualized, never swallowed.** A retry emits
`failed → retrying → running` transitions the diagram actually shows. Every failed
run has a persisted `failure_reason`.

### The pipeline

```
Supervisor → Clarifier → Worker-1 → … → Worker-N → Verifier → Done
                              ▲                        │
                              └──── rework (bounded) ──┘
```

- **Supervisor** decomposes the task into an ordered plan (max 6 subtasks).
- **Clarifier** finds ambiguity and commits to explicit assumptions. There is no
  human in the loop in v1, so it records its assumptions in the log instead of
  blocking.
- **Worker** executes one subtask, sequentially, with the previous subtasks'
  output and the Clarifier's assumptions in context.
- **Verifier** approves the combined output or rejects it with actionable feedback
  that routes back to the responsible Worker.

### Two independent failure budgets

This distinction is the subtle part, and it's deliberate ([TECH_SPEC §3.2](TECH_SPEC.md)):

| Failure class | Example | Budget | Behaviour |
|---|---|---|---|
| Transient technical | timeout, 503, unparseable JSON | `MAX_RETRIES` (3) | Retry with 1s → 3s → 9s backoff |
| Permanent technical | 401, 400 | none | Fail immediately — retrying a 401 just burns 13s of backoff |
| Verifier rejection | output doesn't satisfy the task | `MAX_REWORK_CYCLES` (2) | Route back to the Worker |

A Verifier rejection must never consume the technical retry budget. They are
counted in separate columns (`attempt_count` vs `rework_count`) and surfaced
separately in the UI.

---

## Quick start

### With Docker Compose (recommended)

**Requires Compose v2** (`docker compose`, space not hyphen). The v1 Python
binary silently ignores the `depends_on` conditions this stack relies on, which
would let the API start before migrations have run.

```bash
docker compose version          # want v2.x; install with: sudo apt-get install docker-compose-v2

cp .env.example .env            # defaults work as-is; no API key needed
docker compose up --build
```

- Dashboard → http://localhost:3000
- API docs → http://localhost:8000/docs
- Health → http://localhost:8000/health

Migrations run automatically in a one-shot `migrate` service that both the API
and the worker wait on.

> If you hit `permission denied while trying to connect to the Docker API at
> unix:///var/run/docker.sock`, add yourself to the docker group and start a new
> shell: `sudo usermod -aG docker $USER && newgrp docker`.

### Without Docker

You need a Postgres and a Redis reachable from your machine.

```bash
# --- backend ---
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv/Scripts/activate
pip install -r requirements-dev.txt

export DATABASE_URL='postgresql+asyncpg://agentops:agentops@localhost:5432/agentops'
export REDIS_URL='redis://localhost:6379/0'

alembic upgrade head
uvicorn app.main:app --reload --port 8000    # terminal 1
python worker.py                             # terminal 2 — separate process, on purpose

# --- frontend ---
cd frontend
npm install
npm run dev                                  # terminal 3
```

---

## The LLM provider is mock-only right now

`LLM_PROVIDER=mock` is the only implemented provider, and that is a deliberate
choice rather than an unfinished edge: the orchestrator, the state machine, the
retry paths, and the failure demos all run end-to-end at **zero API cost and with
no key to leak**. It also directly serves the "replay mode" safety requirement
for a public demo in [PRD §10](PRD.md).

What is mocked is only the *transport*. The retry, backoff, timeout, and
error-classification machinery in
[`llm_client.py`](backend/app/orchestrator/llm_client.py) is provider-agnostic and
real, and is covered by tests that assert the exact 1s/3s/9s backoff schedule.

**Adding a real provider** means implementing the `LLMProvider` protocol —
one `async def complete(request) -> LLMResponse` that raises `TransientLLMError`
or `PermanentLLMError` and does not retry. Nothing else changes.

### Demoing retry and failure behaviour

The mock is a fault injector, which is how [PRD §11](PRD.md)'s "deliberately
flaky agent toggle" requirement is met:

```bash
# 40% of LLM calls fail transiently — watch nodes go failed → retrying → running
MOCK_FAILURE_RATE=0.4 docker compose up

# 30% return unparseable prose instead of JSON — exercises the parse-retry path
MOCK_MALFORMED_RATE=0.3 docker compose up

# Guarantee an exhausted-retry failure, to demo the failed end state
MOCK_FAILURE_RATE=1.0 docker compose up

# Reproducible runs (same failures every time)
MOCK_SEED=42 MOCK_FAILURE_RATE=0.4 docker compose up
```

---

## Configuration

All config flows through one `Settings` class
([`core/config.py`](backend/app/core/config.py)); nothing else reads `os.environ`.
Full list in [`.env.example`](.env.example).

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://…` | **Must** use `+asyncpg`. A bare `postgresql://` is rejected at boot with a clear error rather than failing deep inside the first query. |
| `REDIS_URL` | `redis://redis:6379/0` | Queue and pub/sub |
| `LLM_PROVIDER` | `mock` | Only `mock` is implemented |
| `MAX_RETRIES` | `3` | Technical retries per agent |
| `BACKOFF_BASE_SECONDS` / `BACKOFF_MULTIPLIER` | `1.0` / `3.0` | Gives 1s, 3s, 9s |
| `MAX_REWORK_CYCLES` | `2` | Verifier rejections — separate budget |
| `MOCK_FAILURE_RATE` / `MOCK_MALFORMED_RATE` | `0.0` | Fault injection for demos |
| `MOCK_SEED` | *(unset)* | Set for reproducible runs |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated |
| `BACKEND_INTERNAL_URL` | `http://backend:8000` | Server Components only — never sent to the browser |
| `NEXT_PUBLIC_API_URL` / `NEXT_PUBLIC_WS_URL` | `localhost:8000` | Browser-visible. Never put a secret behind `NEXT_PUBLIC_`. |

---

## API

Base: `/api/v1`

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/jobs` | Submit a job → `201` with `job_id` |
| `GET` | `/jobs` | Paginated list; `?status=`, `?limit=`, `?offset=` |
| `GET` | `/jobs/{id}` | Full detail + ordered `agent_runs` |
| `GET` | `/jobs/{id}/logs` | `?since=` cursor for reconnect backfill |
| `GET` | `/health` | Real `SELECT 1` + Redis `PING`, not just "a client object exists" |

WebSocket: `ws://host/ws/jobs/{job_id}` (per-job) and `ws://host/ws/jobs`
(global, for the queue view). Every frame shares one envelope:

```json
{
  "event_type": "agent_status_changed | log_line | job_status_changed",
  "job_id": "uuid",
  "timestamp": "iso8601",
  "payload": { }
}
```

The envelope is defined in **two** places that must change together:
[`schemas.py`](backend/app/models/schemas.py) and
[`events.ts`](frontend/src/lib/events.ts). The TypeScript side is a discriminated
union with an `assertNever` default, so adding an event type to one side without
the other is a compile error.

### Reconnect behaviour

On an unexpected close, the client reconnects with exponential backoff **plus
jitter**, capped at 30s, then backfills `GET /jobs/{id}` and
`GET /jobs/{id}/logs?since=<last_seen>` before resuming. Backfill and the live
stream overlap by design, so log lines are deduplicated by backend `log_id` —
skipping that is what produces duplicate lines in the panel. Once a job is
`completed` or `failed`, reconnection stops: the server closes the channel, and
retrying forever looks broken in the network tab.

---

## Testing

```bash
cd backend  && pytest              # 99 tests
cd frontend && npm test            # 72 tests
```

**Current status — actually run, not aspirational:**

| Check | Result |
|---|---|
| `pytest` | **99 passed** |
| `ruff check` | clean |
| `ruff format --check` | clean |
| `mypy app worker.py` | clean, 24 source files |
| `alembic upgrade head --sql` | renders valid Postgres DDL |
| `npm test` (vitest) | **72 passed** |
| `tsc --noEmit` | clean |
| `next build` | succeeds |

Backend unit tests need no Postgres or Redis: the models carry SQLite dialect
variants (`with_variant`) so tests run against in-memory SQLite, and the event
publisher is an interface with an in-memory double. The migration still emits
native `UUID`, `JSONB`, and Postgres enums for production.

What the tests actually pin down, rather than just exercising:

- The backoff schedule is **exactly** 1s, 3s — and there is no pointless sleep
  after the final attempt
- A `PermanentLLMError` fails on attempt 1 and never sleeps
- An illegal state transition raises *and* emits nothing *and* leaves the row
  untouched
- A Verifier rejection produces **no** `retrying` state — the budgets really are
  separate
- A rework cycle re-runs only the rejected subtask; earlier Workers keep their output
- A Redis outage still persists state (liveness lost, data kept)
- A crashed worker's orphaned `running` job gets reconciled to `failed` at startup
- Malformed WebSocket frames are dropped rather than spread into React state
- `useJobSocket` does not reconnect after a terminal status, and detaches its
  handlers on unmount so a late `onclose` cannot schedule a reconnect

CI additionally runs the migration against a **real Postgres**, tests the
`downgrade` path, and fails if `alembic revision --autogenerate` produces a
non-empty diff (models drifting from migrations).

---

## Project layout

```
backend/
  app/
    main.py                  FastAPI app, lifespan, CORS, /health
    api/jobs.py              REST endpoints
    api/websockets.py        WS handlers (bounded per-client buffer)
    core/config.py           the only place env is read
    core/db.py               async engine; expire_on_commit=False
    core/redis_client.py     client + channel naming
    models/db_models.py      SQLAlchemy 2.0 typed models
    models/schemas.py        Pydantic v2 — REST + event contract
    orchestrator/
      llm_client.py          raw-httpx-shaped client: retry, backoff, JSON extraction
      base.py                Agent ABC + PipelineContext
      supervisor.py clarifier.py worker.py verifier.py
      pipeline.py            the orchestration — read this one
      events.py              emit_event: persist then publish; transition tables
    queue/job_queue.py       BRPOP consumer, graceful shutdown, reconciliation
  worker.py                  separate-process entrypoint
  alembic/                   async env.py + hand-written initial migration
  tests/                     99 tests
frontend/
  src/app/                   App Router pages (Server Components)
  src/components/            PipelineDiagram (hand-rolled SVG), LogPanel, …
  src/hooks/useJobSocket.ts  reconnect + backfill + dedup
  src/lib/                   api.ts, events.ts (contract mirror), status.ts
  tests/                     72 tests
.claude/skills/              project skills encoding these conventions
```

---

## Notable implementation details

Things that were easy to get wrong and are commented at the site:

- **`expire_on_commit=False`** on the session factory. With the default `True`,
  any attribute access after `commit()` triggers a lazy refresh, which raises
  `MissingGreenlet` under asyncio. This is the single most common async
  SQLAlchemy bug.
- **`lazy="raise"`** on every relationship, so an accidental lazy load fails
  loudly in development instead of inside a request.
- **`values_callable`** on the SQLAlchemy enums. Without it, SQLAlchemy persists
  member *names* (`QUEUED`) instead of values (`queued`), breaking the lowercase
  API contract.
- **`create_type=False`** on the migration's enum columns, with explicit
  `.create()` calls. Omitting it causes
  `DuplicateObject: type "job_status" already exists`.
- **Subtask indices are renumbered from position**, not trusted from the model —
  LLMs skip and duplicate indices, and the pipeline keys Worker output by index.
- **The Verifier refuses ambiguous approvals.** `"true"` is coerced; `1`, `"yes"`,
  and `null` raise. Mis-reading an approval would ship unverified output.
- **A rejection with empty feedback is treated as malformed** — a Worker cannot
  act on it.
- **WebSocket handlers use a bounded outbox.** A client that stops reading gets
  dropped rather than applying backpressure to the orchestrator.
- **The socket factory lives in a ref, not the effect deps.** As a dependency it
  reconnected on every render for any caller passing an inline arrow — a real bug
  the reconnect tests caught.

---

## Deployment

Per [TECH_SPEC §9](TECH_SPEC.md):

- **Frontend** → Vercel (standard Next.js; `output: "standalone"` is also set for
  container hosts)
- **Backend + worker** → Render / Railway / Fly.io. Needs a host that supports
  long-lived WebSockets *and* a second always-on process for the queue consumer.
  **Not** pure serverless functions.
- **Postgres / Redis** → managed instances from the same provider, or Neon/Supabase
  + Upstash.

Set `CORS_ORIGINS` to the deployed frontend origin and `NEXT_PUBLIC_WS_URL` to a
`wss://` URL. The WS scheme is otherwise derived from the page protocol, so an
`https` page will not attempt an insecure socket.

---

## Scope

**In v1** — everything in [PRD §7](PRD.md): job submission, live pipeline
visualization, live log streaming with per-agent filtering, job queue view, run
detail view, retry/failure handling.

**Deliberately out** ([PRD §4, §8](PRD.md)): authentication and multi-tenancy,
multi-provider LLM routing, editing pipeline topology from the UI, rate limiting
and billing, mobile-responsive polish (desktop-first is accepted).

Auth is worth being explicit about: it is **not** an oversight. v1 is a
single-user local/demo tool by design, and adding a login flow would expand scope
without demonstrating anything the orchestration work does not already show.

### Known issues

- `npm audit` reports 12 high-severity advisories, **all** transitively from
  `sharp` (libvips CVEs). `sharp` is an optional Next.js dependency used only for
  `next/image` optimization, which this app does not use. The only fix npm offers
  is `--force`, which would downgrade Next.js to 9.3.3 — a far worse outcome.
  Recorded rather than silently ignored.
- Parallel execution of independent subtasks is a v2 item ([TECH_SPEC §3](TECH_SPEC.md));
  v1 Workers run strictly sequentially, since subtask *N* may depend on 1…*N-1*.

---

## Working on this with Claude Code

Project skills in [`.claude/skills/`](.claude/skills/) encode the conventions
above so they survive contact with future edits — the no-framework rule, the
`emit_event` contract, the WS envelope, async SQLAlchemy pitfalls, Alembic enum
ordering, and the `useJobSocket` contract. See [`skills/README.md`](skills/README.md)
for what is authored here versus installed from the marketplace.
