/**
 * The single source of status → colour mapping (PRD §7.2).
 *
 * Imported by the diagram, the queue badges, and the log rows. A second copy of
 * this table anywhere would drift within a week.
 *
 * Every status carries a text label as well as a colour: state must never be
 * encoded by colour alone, or the diagram is unreadable to a colourblind viewer
 * and in a greyscale screenshot.
 */

import type { AgentStatus, JobStatus, LogLevel } from "./events";

export interface StatusStyle {
  /** Human-readable label, always rendered alongside the colour. */
  label: string;
  /** Tailwind classes for a filled badge. */
  badge: string;
  /** Fill for an SVG diagram node. */
  nodeFill: string;
  /** Stroke for an SVG diagram node. */
  nodeStroke: string;
  /** Text colour inside a diagram node. */
  nodeText: string;
  /** True for states that animate (PRD §7.2: running is animated). */
  animated: boolean;
}

export const AGENT_STATUS_STYLES: Record<AgentStatus, StatusStyle> = {
  idle: {
    label: "Idle",
    badge: "bg-slate-700 text-slate-200 border border-slate-600",
    nodeFill: "#334155",
    nodeStroke: "#475569",
    nodeText: "#e2e8f0",
    animated: false,
  },
  queued: {
    label: "Queued",
    badge: "bg-amber-500/20 text-amber-200 border border-amber-500/60",
    nodeFill: "#78350f",
    nodeStroke: "#f59e0b",
    nodeText: "#fde68a",
    animated: false,
  },
  running: {
    label: "Running",
    badge: "bg-blue-500/20 text-blue-200 border border-blue-500/60",
    nodeFill: "#1e3a8a",
    nodeStroke: "#3b82f6",
    nodeText: "#bfdbfe",
    animated: true,
  },
  completed: {
    label: "Completed",
    badge: "bg-emerald-500/20 text-emerald-200 border border-emerald-500/60",
    nodeFill: "#064e3b",
    nodeStroke: "#10b981",
    nodeText: "#a7f3d0",
    animated: false,
  },
  failed: {
    label: "Failed",
    badge: "bg-red-500/20 text-red-200 border border-red-500/60",
    nodeFill: "#7f1d1d",
    nodeStroke: "#ef4444",
    nodeText: "#fecaca",
    animated: false,
  },
  retrying: {
    label: "Retrying",
    badge: "bg-orange-500/20 text-orange-200 border border-orange-500/60",
    nodeFill: "#7c2d12",
    nodeStroke: "#f97316",
    nodeText: "#fed7aa",
    animated: true,
  },
};

export const JOB_STATUS_STYLES: Record<JobStatus, StatusStyle> = {
  queued: AGENT_STATUS_STYLES.queued,
  running: AGENT_STATUS_STYLES.running,
  completed: AGENT_STATUS_STYLES.completed,
  failed: AGENT_STATUS_STYLES.failed,
};

export const LOG_LEVEL_STYLES: Record<LogLevel, { label: string; className: string }> = {
  info: { label: "INFO", className: "text-slate-400" },
  warn: { label: "WARN", className: "text-amber-400" },
  error: { label: "ERROR", className: "text-red-400" },
};

export function agentStatusStyle(status: AgentStatus): StatusStyle {
  return AGENT_STATUS_STYLES[status] ?? AGENT_STATUS_STYLES.idle;
}

export function jobStatusStyle(status: JobStatus): StatusStyle {
  return JOB_STATUS_STYLES[status] ?? AGENT_STATUS_STYLES.idle;
}

/** Terminal job states. The socket must not reconnect once a job reaches one. */
export const TERMINAL_JOB_STATUSES: ReadonlySet<JobStatus> = new Set<JobStatus>([
  "completed",
  "failed",
]);

export function isTerminal(status: JobStatus | null | undefined): boolean {
  return status != null && TERMINAL_JOB_STATUSES.has(status);
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
}
