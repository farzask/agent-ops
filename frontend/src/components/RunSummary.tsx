"use client";

/** Final output, total duration, per-agent breakdown, failure reason (TECH_SPEC §7.2). */

import type { AgentRunDetail } from "@/lib/api";
import type { JobStatus } from "@/lib/events";
import type { ConnectionState } from "@/hooks/useJobSocket";
import { agentStatusStyle, formatDuration, jobStatusStyle } from "@/lib/status";

const CONNECTION_LABELS: Record<ConnectionState, string> = {
  connecting: "Connecting…",
  open: "Live",
  reconnecting: "Reconnecting…",
  closed: "Stream closed",
  error: "Connection error",
};

const CONNECTION_CLASSES: Record<ConnectionState, string> = {
  connecting: "text-amber-300",
  open: "text-emerald-300",
  reconnecting: "text-amber-300",
  closed: "text-slate-400",
  error: "text-red-300",
};

interface RunSummaryProps {
  taskDescription: string;
  jobStatus: JobStatus;
  durationMs: number | null;
  finalOutput: string | null;
  failureReason: string | null;
  agentRuns: AgentRunDetail[];
  connection: ConnectionState;
}

export function RunSummary({
  taskDescription,
  jobStatus,
  durationMs,
  finalOutput,
  failureReason,
  agentRuns,
  connection,
}: RunSummaryProps) {
  const style = jobStatusStyle(jobStatus);
  const totalRetries = agentRuns.reduce(
    (sum, run) => sum + Math.max(0, run.attempt_count - 1),
    0,
  );
  const totalReworks = agentRuns.reduce((sum, run) => sum + run.rework_count, 0);

  return (
    <div className="space-y-4">
      <section className="rounded-lg border border-slate-800 bg-slate-950 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-sm font-semibold text-slate-400">Task</h1>
            <p className="mt-1 break-words text-base text-slate-100">
              {taskDescription}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <span className={`rounded px-2.5 py-1 text-xs font-medium ${style.badge}`}>
              {style.label}
            </span>
            <span
              className={`text-xs ${CONNECTION_CLASSES[connection]}`}
              aria-live="polite"
            >
              {CONNECTION_LABELS[connection]}
            </span>
          </div>
        </div>

        <dl className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
          <div>
            <dt className="text-xs text-slate-500">Total duration</dt>
            <dd className="mt-0.5 font-mono text-sm text-slate-200">
              {formatDuration(durationMs)}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Agents</dt>
            <dd className="mt-0.5 font-mono text-sm text-slate-200">
              {agentRuns.length}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Retries</dt>
            <dd className="mt-0.5 font-mono text-sm text-slate-200">
              {totalRetries}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Rework cycles</dt>
            <dd className="mt-0.5 font-mono text-sm text-slate-200">
              {totalReworks}
            </dd>
          </div>
        </dl>
      </section>

      {failureReason && (
        <section
          role="alert"
          className="rounded-lg border border-red-500/40 bg-red-500/10 p-4"
        >
          <h2 className="text-sm font-semibold text-red-200">Failure reason</h2>
          <p className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-red-200/90">
            {failureReason}
          </p>
        </section>
      )}

      <section className="rounded-lg border border-slate-800 bg-slate-950">
        <h2 className="border-b border-slate-800 px-4 py-2.5 text-sm font-semibold text-slate-200">
          Per-agent breakdown
        </h2>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-slate-500">
              <tr>
                <th scope="col" className="px-4 py-2 font-medium">Agent</th>
                <th scope="col" className="px-4 py-2 font-medium">Status</th>
                <th scope="col" className="px-4 py-2 font-medium">Attempts</th>
                <th scope="col" className="px-4 py-2 font-medium">Reworks</th>
                <th scope="col" className="px-4 py-2 font-medium">Duration</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/70">
              {agentRuns.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-4 py-6 text-center text-slate-600">
                    No agent runs yet.
                  </td>
                </tr>
              ) : (
                [...agentRuns]
                  .sort((a, b) => a.sequence_index - b.sequence_index)
                  .map((run) => {
                    const runStyle = agentStatusStyle(run.status);
                    return (
                      <tr key={run.agent_run_id}>
                        <td className="px-4 py-2 text-slate-200">{run.agent_name}</td>
                        <td className="px-4 py-2">
                          <span
                            className={`rounded px-2 py-0.5 text-[11px] ${runStyle.badge}`}
                          >
                            {runStyle.label}
                          </span>
                        </td>
                        <td className="px-4 py-2 font-mono text-slate-300">
                          {run.attempt_count}
                        </td>
                        <td className="px-4 py-2 font-mono text-slate-300">
                          {run.rework_count}
                        </td>
                        <td className="px-4 py-2 font-mono text-slate-300">
                          {formatDuration(run.duration_ms)}
                        </td>
                      </tr>
                    );
                  })
              )}
            </tbody>
          </table>
        </div>
      </section>

      {finalOutput && (
        <section className="rounded-lg border border-slate-800 bg-slate-950">
          <h2 className="border-b border-slate-800 px-4 py-2.5 text-sm font-semibold text-slate-200">
            Final output
          </h2>
          <pre className="overflow-x-auto whitespace-pre-wrap break-words px-4 py-3 text-xs leading-relaxed text-slate-300">
            {finalOutput}
          </pre>
        </section>
      )}
    </div>
  );
}
