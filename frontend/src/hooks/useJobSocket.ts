"use client";

/**
 * Live event stream for one job, with reconnect and backfill.
 *
 * Implements TECH_SPEC §6 "Reconnection Behavior":
 *   1. track the last-seen timestamp
 *   2. reconnect with exponential backoff + jitter, capped
 *   3. on reconnect, backfill via GET /jobs/{id} and /jobs/{id}/logs?since=
 *   4. deduplicate — backfill and the live stream overlap by design
 *   5. stop reconnecting once the job is terminal
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type {
  AgentOpsEvent,
  AgentStatus,
  JobStatus,
} from "@/lib/events";
import { assertNever, parseEvent } from "@/lib/events";
import {
  fetchJob,
  fetchLogsSince,
  wsBaseUrl,
  type AgentRunDetail,
  type JobDetail,
  type LogEntry,
} from "@/lib/api";
import { isTerminal } from "@/lib/status";

const BASE_RECONNECT_DELAY_MS = 1_000;
const MAX_RECONNECT_DELAY_MS = 30_000;

/** Cap retained log lines so a long run cannot grow memory without bound. */
export const MAX_LOG_LINES = 2_000;

export type ConnectionState =
  | "connecting"
  | "open"
  | "reconnecting"
  | "closed"
  | "error";

export interface JobSocketState {
  jobStatus: JobStatus;
  agentRuns: AgentRunDetail[];
  logs: LogEntry[];
  /** True once MAX_LOG_LINES was exceeded and old lines were dropped. */
  logsTruncated: boolean;
  finalOutput: string | null;
  failureReason: string | null;
  durationMs: number | null;
  connection: ConnectionState;
}

/** Backoff with jitter. Jitter matters when many tabs reconnect at once. */
export function reconnectDelay(attempt: number): number {
  const exponential = BASE_RECONNECT_DELAY_MS * 2 ** attempt;
  const capped = Math.min(exponential, MAX_RECONNECT_DELAY_MS);
  return Math.round(capped * (0.5 + Math.random() * 0.5));
}

/** Merge log lists, dropping duplicates and keeping chronological order. */
export function mergeLogs(existing: LogEntry[], incoming: LogEntry[]): LogEntry[] {
  if (incoming.length === 0) return existing;

  const seen = new Set(existing.map((entry) => entry.log_id));
  const merged = [...existing];
  for (const entry of incoming) {
    // A null/absent id cannot be deduplicated; keep it rather than drop a real
    // line, since the backend always supplies one in practice.
    if (entry.log_id && seen.has(entry.log_id)) continue;
    if (entry.log_id) seen.add(entry.log_id);
    merged.push(entry);
  }

  merged.sort((a, b) => {
    const delta = Date.parse(a.timestamp) - Date.parse(b.timestamp);
    // Ties broken by id so the order is stable across re-renders.
    return delta !== 0 ? delta : a.log_id.localeCompare(b.log_id);
  });
  return merged;
}

/**
 * Apply one agent status change to the run list.
 *
 * Upserts by agent name: the diagram shows one node per agent whose attempt
 * count rises, not a new node per retry.
 */
export function applyAgentStatus(
  runs: AgentRunDetail[],
  payload: {
    agent_run_id: string | null;
    agent_name: string;
    sequence_index: number;
    new_status: AgentStatus;
    attempt_count: number;
    rework_count: number;
    failure_reason: string | null;
  },
): AgentRunDetail[] {
  const index = runs.findIndex((run) => run.agent_name === payload.agent_name);

  if (index === -1) {
    const created: AgentRunDetail = {
      agent_run_id: payload.agent_run_id ?? payload.agent_name,
      agent_name: payload.agent_name,
      sequence_index: payload.sequence_index,
      status: payload.new_status,
      attempt_count: payload.attempt_count,
      rework_count: payload.rework_count,
      started_at: null,
      completed_at: null,
      duration_ms: null,
      output_payload: null,
      failure_reason: payload.failure_reason,
    };
    return [...runs, created].sort((a, b) => a.sequence_index - b.sequence_index);
  }

  const next = [...runs];
  next[index] = {
    ...next[index],
    status: payload.new_status,
    attempt_count: payload.attempt_count,
    rework_count: payload.rework_count,
    failure_reason: payload.failure_reason,
  };
  return next;
}

export interface UseJobSocketOptions {
  initialJob: JobDetail;
  initialLogs: LogEntry[];
  /** Injectable for tests; defaults to the global WebSocket. */
  socketFactory?: (url: string) => WebSocket;
}

export function useJobSocket({
  initialJob,
  initialLogs,
  socketFactory,
}: UseJobSocketOptions): JobSocketState {
  const [jobStatus, setJobStatus] = useState<JobStatus>(initialJob.status);
  const [agentRuns, setAgentRuns] = useState<AgentRunDetail[]>(
    initialJob.agent_runs,
  );
  const [logs, setLogs] = useState<LogEntry[]>(initialLogs);
  const [logsTruncated, setLogsTruncated] = useState(false);
  const [finalOutput, setFinalOutput] = useState(initialJob.final_output);
  const [failureReason, setFailureReason] = useState(initialJob.failure_reason);
  const [durationMs, setDurationMs] = useState(initialJob.duration_ms);
  const [connection, setConnection] = useState<ConnectionState>("connecting");

  const jobId = initialJob.job_id;

  // Refs, not state: putting the socket or attempt counter in state would
  // trigger a reconnect on every render.
  const socketRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastSeenRef = useRef<string | null>(
    initialLogs.length > 0 ? initialLogs[initialLogs.length - 1].timestamp : null,
  );
  const terminalRef = useRef(isTerminal(initialJob.status));
  const mountedRef = useRef(true);

  // The factory is an injection point, not reactive state. Keeping it in the
  // effect's dependency array would tear down and reopen the socket on every
  // render for any caller that passes an inline arrow function.
  const socketFactoryRef = useRef(socketFactory);
  socketFactoryRef.current = socketFactory;

  const appendLogs = useCallback((incoming: LogEntry[]) => {
    if (incoming.length === 0) return;
    setLogs((current) => {
      const merged = mergeLogs(current, incoming);
      if (merged.length <= MAX_LOG_LINES) return merged;
      setLogsTruncated(true);
      return merged.slice(merged.length - MAX_LOG_LINES);
    });
  }, []);

  const handleEvent = useCallback(
    (event: AgentOpsEvent) => {
      if (event.job_id !== jobId) return;

      switch (event.event_type) {
        case "log_line": {
          const { payload } = event;
          appendLogs([
            {
              log_id: payload.log_id ?? `${event.timestamp}:${payload.message}`,
              agent_name: payload.agent_name,
              timestamp: event.timestamp,
              level: payload.level,
              message: payload.message,
            },
          ]);
          lastSeenRef.current = event.timestamp;
          break;
        }
        case "agent_status_changed": {
          setAgentRuns((current) => applyAgentStatus(current, event.payload));
          break;
        }
        case "job_status_changed": {
          const { payload } = event;
          setJobStatus(payload.new_status);
          if (payload.failure_reason) setFailureReason(payload.failure_reason);
          if (payload.duration_ms != null) setDurationMs(payload.duration_ms);
          if (isTerminal(payload.new_status)) {
            terminalRef.current = true;
            // The run is over; fetch the final output the event does not carry.
            void fetchJob(jobId)
              .then((detail) => {
                if (!mountedRef.current) return;
                setFinalOutput(detail.final_output);
                setFailureReason(detail.failure_reason);
                setDurationMs(detail.duration_ms);
                setAgentRuns(detail.agent_runs);
              })
              .catch(() => {
                // Non-fatal: the live state already reflects the outcome.
              });
          }
          break;
        }
        default:
          // Exhaustiveness check: a new event type is a compile error here.
          assertNever(event);
      }
    },
    [appendLogs, jobId],
  );

  /** Reconcile missed state after a gap in the stream (TECH_SPEC §6). */
  const backfill = useCallback(async () => {
    try {
      const [detail, logPage] = await Promise.all([
        fetchJob(jobId),
        fetchLogsSince(jobId, lastSeenRef.current),
      ]);
      if (!mountedRef.current) return;

      setJobStatus(detail.status);
      setAgentRuns(detail.agent_runs);
      setFinalOutput(detail.final_output);
      setFailureReason(detail.failure_reason);
      setDurationMs(detail.duration_ms);
      terminalRef.current = isTerminal(detail.status);

      appendLogs(logPage.logs);
      if (logPage.next_since) lastSeenRef.current = logPage.next_since;
    } catch {
      // Leave the live stream to catch up; a failed backfill is not fatal.
    }
  }, [appendLogs, jobId]);

  useEffect(() => {
    mountedRef.current = true;

    const factory =
      socketFactoryRef.current ??
      ((url: string): WebSocket => new WebSocket(url));

    const connect = (): void => {
      if (!mountedRef.current || terminalRef.current) {
        setConnection("closed");
        return;
      }

      let socket: WebSocket;
      try {
        socket = factory(`${wsBaseUrl()}/ws/jobs/${jobId}`);
      } catch {
        setConnection("error");
        scheduleReconnect();
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        if (!mountedRef.current) return;
        setConnection("open");
        const wasReconnect = attemptRef.current > 0;
        attemptRef.current = 0;
        // Only backfill on a reconnect: the first connect was already seeded
        // with server-rendered state.
        if (wasReconnect) void backfill();
      };

      socket.onmessage = (message: MessageEvent) => {
        if (!mountedRef.current) return;
        if (typeof message.data !== "string") return;
        const event = parseEvent(message.data);
        // A malformed frame is dropped rather than spread into state.
        if (event) handleEvent(event);
      };

      socket.onerror = () => {
        if (!mountedRef.current) return;
        setConnection("error");
      };

      socket.onclose = () => {
        if (!mountedRef.current) return;
        socketRef.current = null;
        if (terminalRef.current) {
          // The server closes the channel on a finished run. Retrying forever
          // wastes cycles and looks broken in the network tab.
          setConnection("closed");
          return;
        }
        setConnection("reconnecting");
        scheduleReconnect();
      };
    };

    const scheduleReconnect = (): void => {
      if (!mountedRef.current || terminalRef.current) return;
      const delay = reconnectDelay(attemptRef.current);
      attemptRef.current += 1;
      timerRef.current = setTimeout(connect, delay);
    };

    connect();

    return () => {
      mountedRef.current = false;
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      const socket = socketRef.current;
      if (socket) {
        // Detach handlers before closing so onclose does not schedule a
        // reconnect for a component that is going away.
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
        socketRef.current = null;
      }
    };
    // socketFactory is deliberately absent: it lives in a ref (see above) so a
    // caller's inline arrow cannot cause a reconnect loop.
  }, [backfill, handleEvent, jobId]);

  return {
    jobStatus,
    agentRuns,
    logs,
    logsTruncated,
    finalOutput,
    failureReason,
    durationMs,
    connection,
  };
}
