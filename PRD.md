# Product Requirements Document (PRD)

## Project Name
**AgentOps** — Multi-Agent Pipeline Observability Dashboard

## Author
Farza

## Status
Draft v1.0

## Date
2026-07-26

---

## 1. Overview

AgentOps is a full-stack web application that visualizes a sequential/supervisor-orchestrated multi-agent AI pipeline in real time. A Next.js frontend displays job queues, live agent execution status, and streaming logs, while a FastAPI backend runs the actual multi-agent orchestration using raw LLM API calls — no LangChain, CrewAI, or similar frameworks.

The project exists to produce a portfolio-grade, resume-differentiating artifact that demonstrates:
- Full-stack engineering ability (Next.js + FastAPI + WebSockets + a job queue)
- Understanding of AI agent orchestration internals, not just framework usage
- Real-time systems design (live status updates, log streaming)

## 2. Problem Statement

Most junior/entry-level candidates who claim "AI agent experience" have only used high-level frameworks (LangChain, CrewAI, AutoGen) as black boxes. Hiring managers in 2026 increasingly want to see candidates who understand what's happening underneath: orchestration logic, state management between agent calls, retries, and failure handling. There is no existing personal project that demonstrates this for the target user, and no tool to *observe* an agent pipeline running step-by-step in real time.

## 3. Goals

- Build a working multi-agent pipeline (Supervisor + N worker agents) using only raw LLM API calls.
- Build a job queue system that accepts pipeline run requests and executes them asynchronously.
- Stream live agent status changes (queued → running → completed/failed) to the browser via WebSockets.
- Stream live logs (per-agent, per-step) to the browser as they are generated.
- Present all of the above in a clean, professional Next.js dashboard suitable for a portfolio demo video and interview walkthrough.

## 4. Non-Goals

- Not building a general-purpose agent framework for others to use.
- Not supporting multiple concurrent LLM providers in v1 (single provider, swappable via config).
- Not building user authentication/multi-tenancy in v1 (single-user local/demo tool).
- Not optimizing for massive scale (this is a portfolio project, not a production SaaS).
- Not persisting long-term historical analytics/dashboards in v1 (basic run history only).

## 5. Target User / Audience

- Primary: Farza himself — as the builder and as the person demoing this in interviews.
- Secondary: Recruiters/hiring managers viewing a deployed demo or a walkthrough video/GIF in a resume/portfolio link.

## 6. Use Cases / User Stories

1. **As the developer**, I want to submit a new pipeline job (e.g., "Generate a blog post about X") so that I can watch the Supervisor agent break it down and delegate to worker agents.
2. **As the developer**, I want to see a visual pipeline diagram with each agent's current status (idle/queued/running/completed/failed) updating live, so I can demonstrate orchestration visually.
3. **As the developer**, I want to see a live-streaming log panel per agent so I can show exactly what prompt/response is happening at each step.
4. **As the developer**, I want to view a history of past pipeline runs with their final outputs and duration, so I can show reliability and observability.
5. **As the developer**, I want to see failure/retry behavior visualized (e.g., an agent fails, retries, then succeeds or escalates) so I can demonstrate error handling design.
6. **As a recruiter/viewer**, I want to load a hosted demo and immediately understand what's happening without reading documentation.

## 7. Core Features (v1 Scope)

### 7.1 Job Submission
- A form to submit a new pipeline job with a task description/prompt.
- Job enters a queue; UI immediately shows it as "Queued."

### 7.2 Agent Pipeline Visualization
- Visual diagram (nodes + edges) showing: Supervisor → Clarifier → Planner → Worker(s) → Verifier → Done (exact agent set defined in Tech Spec).
- Each node's color/state updates live: idle (gray), queued (yellow), running (blue, animated), completed (green), failed (red).

### 7.3 Live Log Streaming
- A log panel that streams structured log lines (timestamp, agent name, event type, message) as they're emitted by the backend.
- Filterable by agent.

### 7.4 Job Queue View
- List of all jobs with status, submitted time, duration, and a link to view details.

### 7.5 Run Detail View
- For a single run: full pipeline diagram + full log history + final output + retry events + total duration and per-agent duration breakdown.

### 7.6 Basic Retry/Failure Handling
- If an agent call fails (timeout, malformed output, API error), the backend retries with backoff up to a configured limit, then marks the agent/run as failed if exhausted. This is visualized, not hidden.

## 8. Out of Scope (v1)

- User accounts/login
- Multi-provider LLM routing
- Editing pipeline structure via UI (pipeline topology is defined in backend code for v1)
- Rate limiting / billing
- Mobile-responsive polish (desktop-first is acceptable for v1)

## 9. Success Metrics

Since this is a portfolio project rather than a commercial product, success is measured by:
- The pipeline runs end-to-end reliably (>95% of demo runs complete without unhandled crashes).
- Real-time updates have perceptible latency under ~500ms from backend event to UI update.
- The project is deployable to a public URL for recruiters to view.
- The README/demo clearly documents the "no framework, raw API calls" architecture decision.
- Farza can verbally explain every architectural decision in an interview without hesitation.

## 10. Risks & Open Questions

| Risk | Mitigation |
|---|---|
| LLM API costs during demoing/development | Use a cheap/small model for dev; cache demo runs; consider a "replay mode" using recorded runs for the public demo to avoid live API cost/key exposure |
| WebSocket reliability across deploy targets (e.g., serverless Next.js hosts) | Confirm hosting choice early (see Tech Spec §Deployment); consider a separate long-running host for the FastAPI/WebSocket server |
| Scope creep (wanting to add a pipeline-builder UI, multi-provider support, etc.) | Explicitly time-box v1 to the features in §7; log extra ideas as v2 backlog |
| Exposing LLM API keys in a public demo | Backend-only key usage; public demo runs in "replay mode" or rate-limited "live mode" behind a demo API key with hard spend caps |

## 11. Milestones (High-Level)

1. **M1** — FastAPI backend: job queue + Supervisor/worker agent logic working via CLI/logs only (no frontend yet).
2. **M2** — WebSocket layer added to backend; verify live status/log events emit correctly (test with a simple HTML page or `wscat`).
3. **M3** — Next.js frontend: job submission form + job queue list, connected to backend REST endpoints.
4. **M4** — Next.js frontend: pipeline visualization + live log panel, connected via WebSocket.
5. **M5** — Run detail view + retry/failure visualization + polish.
6. **M6** — Deployment (backend + frontend) + README + demo video/GIF for resume/portfolio.

## 12. Appendix

See `TECH_SPEC.md` for architecture, data models, API contracts, and WebSocket event schemas.
