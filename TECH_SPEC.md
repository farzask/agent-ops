# Technical Requirements Document (TRD)

## Project Name
**AgentOps** — Multi-Agent Pipeline Observability Dashboard

## Companion Document
See `PRD.md` for product scope, goals, and use cases. This document defines architecture, data models, API contracts, and implementation requirements.

---

## 1. Architecture Overview

```
┌─────────────────────┐         REST (job submit,          ┌──────────────────────────┐
│                      │         history fetch)             │                          │
│   Next.js Frontend   │ ──────────────────────────────────▶│    FastAPI Backend       │
│                      │◀────────────────────────────────── │                          │
│  - Dashboard UI      │         WebSocket (live events)     │  - Job Queue             │
│  - Pipeline diagram  │◀═══════════════════════════════════│  - Orchestrator          │
│  - Log viewer        │         (status + log events)       │  - Agent workers         │
│                      │                                     │  - Raw LLM API calls     │
└─────────────────────┘                                     └───────────┬──────────────┘
                                                                          │
                                                              ┌───────────▼──────────────┐
                                                              │  Postgres (jobs, runs,    │
                                                              │  agent_events, logs)      │
                                                              └───────────────────────────┘
                                                                          │
                                                              ┌───────────▼──────────────┐
                                                              │  Redis (queue + pub/sub   │
                                                              │  for WebSocket fan-out)   │
                                                              └───────────────────────────┘
                                                                          │
                                                              ┌───────────▼──────────────┐
                                                              │  LLM Provider API         │
                                                              │  (raw HTTPS calls only)   │
                                                              └───────────────────────────┘
```

### 1.1 Architectural Principles
- **No agent frameworks.** No LangChain, CrewAI, AutoGen, LlamaIndex agents, etc. All prompt construction, response parsing, and control flow are hand-written.
- **Supervisor pattern.** A single Supervisor agent receives the task, decomposes it, and dispatches to worker agents sequentially (or in parallel where independent), then a Verifier agent checks final output before marking the run complete.
- **Event-driven observability.** Every state transition (job queued, agent started, agent completed, agent failed, retry attempted, log line emitted) is published as an event. The event bus is the single source of truth for what the frontend renders — the UI does not poll or infer state.
- **Backend owns all LLM calls.** The frontend never talks to the LLM provider directly and never receives API keys.

---

## 2. Tech Stack

### Frontend
- **Next.js 14+** (App Router)
- **TypeScript**
- **Tailwind CSS** for styling
- **React Server Components** for initial data (job history), **Client Components** for live/interactive views (pipeline diagram, log stream)
- **WebSocket client**: native `WebSocket` API or a thin wrapper (e.g., `reconnecting-websocket`) for auto-reconnect
- **Visualization**: hand-rolled SVG/HTML+CSS diagram for the pipeline graph (no heavy graph library needed given fixed topology); optionally `reactflow` if node positions need to be dynamic later

### Backend
- **FastAPI** (Python 3.11+)
- **Uvicorn** (ASGI server, required for WebSocket support)
- **Raw HTTP client** for LLM calls: `httpx` (async)
- **Postgres** via `SQLAlchemy` (async) + `asyncpg`, or lighter-weight `databases` library
- **Redis** for:
  - Job queue (simple list/stream-based queue; `arq` or hand-rolled with `redis-py` async client — no Celery needed at this scale)
  - Pub/Sub channel to fan out events to all connected WebSocket clients (supports multiple backend workers later)
- **Pydantic v2** for request/response schemas and internal event schemas

### Infra / Dev Tooling
- **Docker Compose** for local dev: `frontend`, `backend`, `postgres`, `redis` services
- **GitHub Actions** for basic CI (lint + type-check + backend tests) — ties into the CI/CD skill area of the broader learning roadmap
- **.env**-based config for LLM provider key, DB URL, Redis URL

---

## 3. Agent Pipeline Design (v1 Topology)

Fixed sequential/supervisor topology for v1:

1. **Supervisor** — receives the raw task, produces a structured plan (ordered list of subtasks with the responsible agent for each).
2. **Clarifier** — checks the plan for ambiguity; may inject clarifying assumptions (no human-in-the-loop in v1; it self-resolves and logs its assumption).
3. **Worker(s)** — one or more agents that execute subtasks from the plan sequentially. v1 ships with a single generic **Worker** agent type that executes each subtask in order (parallelism across independent subtasks is a v2 enhancement).
4. **Verifier** — reviews the combined worker output against the original task and either approves (→ Done) or requests rework (→ back to the responsible Worker step, bounded by max retry count).
5. **Done** — terminal state; final output persisted.

### 3.1 State Machine (per agent node)
```
idle → queued → running → (completed | failed)
failed → retrying → running   (up to MAX_RETRIES, e.g. 3)
failed (retries exhausted) → run marked "failed", pipeline halts
```

### 3.2 Retry & Failure Policy
- Per-agent retry limit: 3 attempts, exponential backoff (e.g., 1s, 3s, 9s).
- Failure classes handled explicitly: LLM API error/timeout, malformed/unparseable JSON response, Verifier rejection (routes back to Worker, counted separately from technical failures).
- On exhausted retries, the run's overall status becomes `failed`, and the reason is persisted and shown in the Run Detail View.

---

## 4. Data Models

### 4.1 Postgres Schema

**`jobs`**
| column | type | notes |
|---|---|---|
| id | UUID (PK) | |
| task_description | text | user-submitted prompt |
| status | enum: queued, running, completed, failed | |
| created_at | timestamptz | |
| started_at | timestamptz, nullable | |
| completed_at | timestamptz, nullable | |
| final_output | text, nullable | |
| failure_reason | text, nullable | |

**`agent_runs`**
| column | type | notes |
|---|---|---|
| id | UUID (PK) | |
| job_id | UUID (FK → jobs.id) | |
| agent_name | text | e.g. "Supervisor", "Worker-1" |
| sequence_index | int | order within the pipeline |
| status | enum: idle, queued, running, completed, failed, retrying | |
| attempt_count | int | |
| started_at | timestamptz, nullable | |
| completed_at | timestamptz, nullable | |
| input_payload | jsonb | prompt/context sent to the agent |
| output_payload | jsonb, nullable | parsed structured response |

**`agent_logs`**
| column | type | notes |
|---|---|---|
| id | UUID (PK) | |
| job_id | UUID (FK) | |
| agent_run_id | UUID (FK, nullable) | null for job-level logs |
| timestamp | timestamptz | |
| level | enum: info, warn, error | |
| message | text | |

### 4.2 Indexes
- `jobs(status, created_at)` — for job queue list view sorted/filtered by status.
- `agent_runs(job_id, sequence_index)` — for ordered pipeline reconstruction.
- `agent_logs(job_id, timestamp)` — for chronological log retrieval and pagination.

---

## 5. API Contracts (REST)

Base URL: `/api/v1`

### `POST /jobs`
Submit a new pipeline job.
**Request:**
```json
{ "task_description": "Write a 500-word blog post about IoT water leak detection" }
```
**Response `201`:**
```json
{ "job_id": "uuid", "status": "queued", "created_at": "iso8601" }
```

### `GET /jobs`
List jobs (paginated), for the Job Queue View.
**Query params:** `status` (optional filter), `limit`, `offset`
**Response `200`:**
```json
{
  "jobs": [
    { "job_id": "uuid", "status": "completed", "created_at": "...", "duration_ms": 8213 }
  ],
  "total": 42
}
```

### `GET /jobs/{job_id}`
Full run detail: job metadata + ordered agent_runs + final output.
**Response `200`:**
```json
{
  "job_id": "uuid",
  "status": "completed",
  "task_description": "...",
  "final_output": "...",
  "agent_runs": [
    { "agent_name": "Supervisor", "status": "completed", "attempt_count": 1, "duration_ms": 1200 }
  ]
}
```

### `GET /jobs/{job_id}/logs`
Paginated log fetch (used for initial load; live updates come via WebSocket).
**Query params:** `since` (timestamp cursor), `limit`

### `GET /health`
Basic liveness check (backend + Redis + Postgres connectivity).

---

## 6. WebSocket Contract

### Endpoint
`ws://<backend-host>/ws/jobs/{job_id}`

Client connects after submitting a job (or when viewing an in-progress/historical run) to receive live events scoped to that job. A global `ws://<backend-host>/ws/jobs` endpoint (no job_id) may be used for the Job Queue View to receive lightweight status-change events across all jobs.

### Event Schema (all events share this envelope)
```json
{
  "event_type": "agent_status_changed | log_line | job_status_changed",
  "job_id": "uuid",
  "timestamp": "iso8601",
  "payload": { }
}
```

### `agent_status_changed` payload
```json
{
  "agent_name": "Worker-1",
  "sequence_index": 2,
  "previous_status": "running",
  "new_status": "completed",
  "attempt_count": 1
}
```

### `log_line` payload
```json
{
  "agent_name": "Supervisor",
  "level": "info",
  "message": "Decomposed task into 3 subtasks"
}
```

### `job_status_changed` payload
```json
{
  "previous_status": "running",
  "new_status": "completed"
}
```

### Delivery Mechanism
- Backend publishes events to a Redis Pub/Sub channel keyed by `job_id` (e.g., `job:{job_id}:events`) at the moment they occur (agent state transition, log emission).
- A WebSocket connection handler subscribes to the relevant Redis channel(s) and forwards messages to the connected client(s).
- This decouples the orchestrator (which may run in a separate worker process) from the WebSocket-serving process, and supports horizontal scaling of the FastAPI app later without losing events.

### Reconnection Behavior
- Frontend uses exponential backoff reconnect.
- On reconnect, frontend calls `GET /jobs/{job_id}` and `GET /jobs/{job_id}/logs?since=<last_seen_timestamp>` to backfill any missed events, then resumes live streaming.

---

## 7. Frontend Requirements

### 7.1 Pages (App Router)
- `/` — Dashboard: job submission form + live Job Queue View
- `/jobs/[jobId]` — Run Detail View: pipeline diagram + log panel + final output

### 7.2 Components
- `JobSubmitForm` — controlled form, POSTs to `/api/v1/jobs`, redirects to `/jobs/[jobId]` on success
- `JobQueueList` — table/list of jobs, subscribes to global WS endpoint for live status badges
- `PipelineDiagram` — renders fixed node topology (Supervisor → Clarifier → Worker(s) → Verifier → Done), colors nodes by live status, animates the "running" state
- `LogPanel` — virtualized/scrollable log list, appends new `log_line` events in real time, supports per-agent filter dropdown
- `RunSummary` — displays final output, total duration, per-agent duration breakdown, failure reason if applicable

### 7.3 State Management
- Server-fetched initial state via Server Components / route handlers.
- Client-side state for live updates managed via React state + a WebSocket hook (`useJobSocket(jobId)`), no external state library required at this scope (Zustand acceptable if state sharing across components gets unwieldy).

### 7.4 Non-Functional
- Perceived update latency target: under 500ms from backend event emission to UI paint.
- Desktop-first responsive layout (v1); mobile polish is out of scope per PRD.

---

## 8. Backend Requirements

### 8.1 Modules
```
backend/
  app/
    main.py                # FastAPI app, router mounting
    api/
      jobs.py               # REST endpoints
      websockets.py         # WS endpoint handlers
    core/
      config.py             # env/config loading
      redis_client.py
      db.py
    orchestrator/
      supervisor.py
      clarifier.py
      worker.py
      verifier.py
      pipeline.py           # sequencing/state machine logic
      llm_client.py         # raw httpx calls to LLM provider, retry/backoff
    models/
      db_models.py          # SQLAlchemy models
      schemas.py            # Pydantic request/response/event schemas
    queue/
      job_queue.py          # Redis-backed queue consumer
  tests/
  requirements.txt
  Dockerfile
```

### 8.2 Job Queue
- On `POST /jobs`, job row inserted with `status=queued`, and a message pushed to a Redis list/stream.
- A background worker process (separate from the request-handling process, run via `uvicorn` + a simple asyncio consumer loop, or a lightweight worker script) pops jobs and runs the orchestrator pipeline.
- This separation matters: it demonstrates understanding of decoupling request handling from long-running task execution — a concept directly reusable in real job interviews.

### 8.3 LLM Client Requirements
- All calls go through `orchestrator/llm_client.py`.
- Must implement: request construction, response parsing (expecting structured JSON from the model — enforce via prompt + validation), timeout handling, retry with exponential backoff, and error classification (transient vs. permanent failure).
- No SDK-level agent abstractions — direct HTTPS calls to the provider's completion endpoint.

### 8.4 Event Emission
- Every state transition in `pipeline.py` must call a shared `emit_event()` function that: (1) persists the event/log to Postgres, and (2) publishes to the relevant Redis Pub/Sub channel.

---

## 9. Deployment

- **Frontend**: Vercel (standard Next.js hosting).
- **Backend**: A host that supports long-lived WebSocket connections and a background worker process — e.g., Render, Railway, or Fly.io (avoid pure serverless functions for the WebSocket/queue-consumer components).
- **Postgres/Redis**: Managed instances via the same provider (Render/Railway both offer these) or a free-tier Postgres (e.g., Supabase/Neon) + Redis (e.g., Upstash) combo.
- **Public Demo Safety**: public-facing deployment runs in a "replay mode" (pre-recorded event sequences replayed over the same WebSocket contract) or a rate-limited "live mode" using a demo LLM API key with a hard monthly spend cap, to avoid cost/key exposure risk noted in the PRD.

---

## 10. Testing Requirements

- **Backend unit tests**: LLM client retry/backoff logic, pipeline state machine transitions, event emission correctness.
- **Backend integration tests**: full pipeline run against a mocked LLM client, asserting correct sequence of persisted `agent_runs` and emitted events.
- **Frontend**: component tests for `PipelineDiagram` status-color mapping and `LogPanel` append behavior; a basic WebSocket mock for testing reconnect logic.

---

## 11. Definition of Done (v1)

- A job can be submitted from the UI and runs to completion (or documented failure) end-to-end.
- Pipeline diagram and log panel update live via WebSocket with no manual refresh.
- Run history is viewable after the fact with full log and output detail.
- Retry/failure behavior can be demonstrated (e.g., via a deliberately flaky mock agent toggle for demo purposes).
- Project is deployed and reachable via a public URL.
- README documents architecture, setup instructions, and explicitly calls out the "raw API calls, no agent framework" design decision.
