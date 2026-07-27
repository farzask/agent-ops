/**
 * WebSocket event types — the TypeScript mirror of TECH_SPEC §6.
 *
 * This file and `backend/app/models/schemas.py` are two halves of one contract.
 * Changing a field in one without the other breaks the wire silently, so change
 * both (and TECH_SPEC §6) in the same commit.
 */

export type JobStatus = "queued" | "running" | "completed" | "failed";

export type AgentStatus =
  | "idle"
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "retrying";

export type LogLevel = "info" | "warn" | "error";

export interface AgentStatusChangedPayload {
  agent_run_id: string | null;
  agent_name: string;
  sequence_index: number;
  previous_status: AgentStatus | null;
  new_status: AgentStatus;
  attempt_count: number;
  rework_count: number;
  failure_reason: string | null;
}

export interface LogLinePayload {
  log_id: string | null;
  agent_name: string | null;
  level: LogLevel;
  message: string;
}

export interface JobStatusChangedPayload {
  previous_status: JobStatus | null;
  new_status: JobStatus;
  task_description: string | null;
  failure_reason: string | null;
  duration_ms: number | null;
}

interface Envelope {
  job_id: string;
  timestamp: string;
}

/**
 * Discriminated union on `event_type`. A `switch` over this is exhaustively
 * checked — see `assertNever` below.
 */
export type AgentOpsEvent =
  | (Envelope & {
      event_type: "agent_status_changed";
      payload: AgentStatusChangedPayload;
    })
  | (Envelope & { event_type: "log_line"; payload: LogLinePayload })
  | (Envelope & {
      event_type: "job_status_changed";
      payload: JobStatusChangedPayload;
    });

export type AgentOpsEventType = AgentOpsEvent["event_type"];

/**
 * Put this in a switch's default branch. Adding a new event type to the union
 * then becomes a compile error rather than a silently ignored message.
 */
export function assertNever(value: never): never {
  throw new Error(`unhandled event type: ${JSON.stringify(value)}`);
}

const EVENT_TYPES: ReadonlySet<string> = new Set([
  "agent_status_changed",
  "log_line",
  "job_status_changed",
]);

/**
 * Narrow an untrusted parsed message to an event.
 *
 * Anything arriving over a socket is `unknown`, not `any` — a malformed frame
 * from a version-skewed backend must be dropped, not spread into React state
 * where it renders as `undefined`.
 */
export function parseEvent(raw: string): AgentOpsEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }

  if (typeof parsed !== "object" || parsed === null) return null;

  const candidate = parsed as Record<string, unknown>;
  if (typeof candidate.event_type !== "string") return null;
  if (!EVENT_TYPES.has(candidate.event_type)) return null;
  if (typeof candidate.job_id !== "string") return null;
  if (typeof candidate.timestamp !== "string") return null;
  if (typeof candidate.payload !== "object" || candidate.payload === null) {
    return null;
  }

  return parsed as AgentOpsEvent;
}

/**
 * Stable identity for deduplication. Backfill (REST) and the live stream
 * overlap by design after a reconnect, so the log panel must be able to tell
 * whether it has already seen a line.
 */
export function eventKey(event: AgentOpsEvent): string {
  if (event.event_type === "log_line" && event.payload.log_id) {
    return `log:${event.payload.log_id}`;
  }
  if (event.event_type === "agent_status_changed") {
    return `agent:${event.payload.agent_name}:${event.payload.new_status}:${event.timestamp}`;
  }
  return `${event.event_type}:${event.timestamp}`;
}
