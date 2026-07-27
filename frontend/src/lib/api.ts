/**
 * Typed REST client for the endpoints in TECH_SPEC §5.
 *
 * Two base URLs exist deliberately (see .claude/skills/nextjs-ui):
 *   - `BACKEND_INTERNAL_URL` for Server Components (never reaches the browser)
 *   - `NEXT_PUBLIC_API_URL`  for Client Components
 */

import type { AgentStatus, JobStatus, LogLevel } from "./events";

export interface JobSummary {
  job_id: string;
  task_description: string;
  status: JobStatus;
  created_at: string;
  duration_ms: number | null;
}

export interface JobListResponse {
  jobs: JobSummary[];
  total: number;
}

export interface AgentRunDetail {
  agent_run_id: string;
  agent_name: string;
  sequence_index: number;
  status: AgentStatus;
  attempt_count: number;
  rework_count: number;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  output_payload: Record<string, unknown> | null;
  failure_reason: string | null;
}

export interface JobDetail {
  job_id: string;
  status: JobStatus;
  task_description: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  final_output: string | null;
  failure_reason: string | null;
  agent_runs: AgentRunDetail[];
}

export interface LogEntry {
  log_id: string;
  agent_name: string | null;
  timestamp: string;
  level: LogLevel;
  message: string;
}

export interface LogListResponse {
  logs: LogEntry[];
  next_since: string | null;
}

export interface CreateJobResponse {
  job_id: string;
  status: JobStatus;
  created_at: string;
}

const API_PREFIX = "/api/v1";

/** Server-side base URL. Falls back to the public one for local dev. */
export function serverBaseUrl(): string {
  return (
    process.env.BACKEND_INTERNAL_URL ??
    process.env.NEXT_PUBLIC_API_URL ??
    "http://localhost:8000"
  );
}

/** Browser-side base URL. */
export function clientBaseUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
}

/**
 * WebSocket base URL, with the scheme derived from the page protocol.
 *
 * Hardcoding `ws:` breaks the deployed build the moment it is served over TLS,
 * because a browser refuses an insecure socket from an https page.
 */
export function wsBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_WS_URL;
  if (configured) return configured;
  if (typeof window === "undefined") return "ws://localhost:8000";
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.hostname}:8000`;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${base}${API_PREFIX}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    // The dashboard is a live view; a cached job list would show stale state.
    cache: "no-store",
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // Non-JSON error body; the status text is the best we have.
    }
    throw new ApiError(detail, response.status);
  }

  return (await response.json()) as T;
}

// --- Server-side (Server Components) --------------------------------------

export function fetchJobsServer(params?: {
  status?: JobStatus;
  limit?: number;
  offset?: number;
}): Promise<JobListResponse> {
  const query = new URLSearchParams();
  if (params?.status) query.set("status", params.status);
  if (params?.limit != null) query.set("limit", String(params.limit));
  if (params?.offset != null) query.set("offset", String(params.offset));
  const suffix = query.size > 0 ? `?${query}` : "";
  return request<JobListResponse>(serverBaseUrl(), `/jobs${suffix}`);
}

export function fetchJobServer(jobId: string): Promise<JobDetail> {
  return request<JobDetail>(serverBaseUrl(), `/jobs/${jobId}`);
}

export function fetchLogsServer(jobId: string, limit = 500): Promise<LogListResponse> {
  return request<LogListResponse>(
    serverBaseUrl(),
    `/jobs/${jobId}/logs?limit=${limit}`,
  );
}

// --- Client-side (Client Components) --------------------------------------

export function createJob(taskDescription: string): Promise<CreateJobResponse> {
  return request<CreateJobResponse>(clientBaseUrl(), "/jobs", {
    method: "POST",
    body: JSON.stringify({ task_description: taskDescription }),
  });
}

export function fetchJob(jobId: string): Promise<JobDetail> {
  return request<JobDetail>(clientBaseUrl(), `/jobs/${jobId}`);
}

/** Backfill after a reconnect (TECH_SPEC §6 "Reconnection Behavior"). */
export function fetchLogsSince(
  jobId: string,
  since: string | null,
): Promise<LogListResponse> {
  const suffix = since ? `?since=${encodeURIComponent(since)}` : "";
  return request<LogListResponse>(clientBaseUrl(), `/jobs/${jobId}/logs${suffix}`);
}

export function fetchJobs(params?: {
  status?: JobStatus;
  limit?: number;
}): Promise<JobListResponse> {
  const query = new URLSearchParams();
  if (params?.status) query.set("status", params.status);
  if (params?.limit != null) query.set("limit", String(params.limit));
  const suffix = query.size > 0 ? `?${query}` : "";
  return request<JobListResponse>(clientBaseUrl(), `/jobs${suffix}`);
}
