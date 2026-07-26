---
name: agentops-conventions
description: Non-negotiable AgentOps project rules — the no-agent-framework constraint, the emit_event() observability contract, the WebSocket event envelope, and the agent state machine. Use this whenever adding or modifying an agent, a pipeline state transition, an event emission, a WebSocket message, or a dependency in this repo. Read BEFORE adding any Python or TypeScript dependency.
---

# AgentOps Conventions

These rules come from `PRD.md` and `TECH_SPEC.md` at the repo root. They are the
things that make this project worth showing to a hiring manager. Breaking them
silently destroys the point of the project.

## Rule 1 — No agent frameworks. Ever.

TECH_SPEC §1.1 and §8.3. **All** prompt construction, response parsing, retry
logic, and control flow is hand-written.

**Banned dependencies** (do not add, do not suggest, do not import):

- `langchain`, `langchain-*`, `langgraph`
- `crewai`, `autogen`, `pyautogen`, `ag2`
- `llama-index`, `llama_index` (agent modules)
- `haystack-ai`, `semantic-kernel`, `griffe`-based agent kits
- `pydantic-ai`, `instructor`, `marvin`, `dspy`
- `openai`, `anthropic`, or any vendor **SDK** — LLM calls are raw `httpx`

Allowed for LLM work: `httpx` only. Pydantic is allowed **for schema validation
of requests/responses/events**, which is not agent orchestration.

If a task seems to need a framework, hand-write it instead. That is the project.

### Why this matters
The README's headline claim is "raw API calls, no agent framework." A single
`langchain` import in `requirements.txt` makes that claim a lie, and it is the
first thing a technical interviewer will grep for.

## Rule 2 — The event bus is the single source of truth

TECH_SPEC §1.1 and §8.4. The frontend never polls and never infers state.

Every state transition **must** go through `emit_event()` in
`backend/app/orchestrator/events.py`, which does exactly two things:

1. Persists to Postgres (`agent_runs` status update and/or an `agent_logs` row)
2. Publishes JSON to the Redis Pub/Sub channel `job:{job_id}:events`

Never mutate `agent_runs.status` or `jobs.status` with a bare SQL update.
Never `redis.publish()` directly from agent or pipeline code.
Never emit an event without persisting it — a client that reconnects backfills
from Postgres, so an unpersisted event is a permanently lost event.

## Rule 3 — The WebSocket envelope is a fixed contract

TECH_SPEC §6. Every message on the wire is exactly this shape:

```json
{
  "event_type": "agent_status_changed | log_line | job_status_changed",
  "job_id": "uuid",
  "timestamp": "iso8601",
  "payload": {}
}
```

Payloads are defined in TECH_SPEC §6 and mirrored in
`backend/app/models/schemas.py` and `frontend/src/lib/events.ts`.

**Changing this contract means changing three places in the same commit:**
the Pydantic event models, the TypeScript types, and TECH_SPEC §6.
Do not add a field to one side only.

Channel naming: `job:{job_id}:events` for per-job, `jobs:events` for the
global queue-view feed. Do not invent other channel names.

## Rule 4 — The agent state machine

TECH_SPEC §3.1. Legal transitions only:

```
idle → queued → running → completed
                running → failed
                failed  → retrying → running   (while attempt_count < MAX_RETRIES)
                failed  → (retries exhausted) → run status = failed, pipeline halts
```

`idle → running` is illegal (must pass through `queued`).
`completed → anything` is illegal (terminal).
Enforce this in `pipeline.py`, not by convention — an illegal transition should
raise, and there is a unit test asserting that.

## Rule 5 — Failure classes are distinct

TECH_SPEC §3.2. Three separate things, never conflated:

| Class | Example | Counts against |
|---|---|---|
| Transient technical | timeout, 429, 5xx | `attempt_count`, retried with backoff |
| Permanent technical | 400, 401, unparseable after retries | fails immediately, no retry |
| Verifier rejection | output doesn't satisfy the task | `rework_count`, routes back to Worker |

Verifier rejections are **not** technical failures and must not consume the
technical retry budget. They have their own bounded counter.

Backoff is exponential: 1s, 3s, 9s (`BACKOFF_BASE_SECONDS * 3**attempt`).

## Rule 6 — Retries and failures are visualized, not hidden

PRD §7.6. A retry emits events. A failure emits events. The UI shows them.
Never swallow an exception without an `emit_event()` carrying the reason.
`jobs.failure_reason` must be populated on every failed run.

## Rule 7 — The backend owns all LLM access

TECH_SPEC §1.1. The frontend never holds a key, never calls a provider, never
receives raw provider responses. All calls funnel through
`orchestrator/llm_client.py`.

## Current implementation status

The LLM provider is **mock-only** right now (`LLM_PROVIDER=mock`), a deliberate
decision so the orchestrator, retry logic, and failure demos run at zero API
cost. The retry/backoff/timeout/error-classification machinery in `llm_client.py`
is provider-agnostic and fully real — only the transport is mocked. Adding a real
provider means implementing the `LLMProvider` protocol; do not restructure the
retry logic to do it.

## Out of scope for v1 — do not add

PRD §4 and §8, confirmed by the project owner:

- Authentication, user accounts, multi-tenancy
- Multi-provider LLM routing
- Editing pipeline topology from the UI
- Rate limiting, billing
- Mobile-responsive polish (desktop-first is accepted)

If a change requires one of these, stop and ask rather than expanding scope.
