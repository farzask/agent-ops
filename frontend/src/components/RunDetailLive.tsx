"use client";

/**
 * Client shell for the run detail view.
 *
 * Seeded with server-rendered state and props-drilled into the hook, so there is
 * no loading spinner on first paint — the whole point of keeping the page itself
 * a Server Component.
 */

import { useJobSocket } from "@/hooks/useJobSocket";
import { LogPanel } from "@/components/LogPanel";
import { PipelineDiagram } from "@/components/PipelineDiagram";
import { RunSummary } from "@/components/RunSummary";
import type { JobDetail, LogEntry } from "@/lib/api";
import { isTerminal } from "@/lib/status";

interface RunDetailLiveProps {
  initialJob: JobDetail;
  initialLogs: LogEntry[];
}

export function RunDetailLive({ initialJob, initialLogs }: RunDetailLiveProps) {
  const {
    jobStatus,
    agentRuns,
    logs,
    logsTruncated,
    finalOutput,
    failureReason,
    durationMs,
    connection,
  } = useJobSocket({ initialJob, initialLogs });

  return (
    <div className="space-y-4">
      <RunSummary
        taskDescription={initialJob.task_description}
        jobStatus={jobStatus}
        durationMs={durationMs}
        finalOutput={finalOutput}
        failureReason={failureReason}
        agentRuns={agentRuns}
        connection={connection}
      />

      <section className="rounded-lg border border-slate-800 bg-slate-950 p-4">
        <h2 className="mb-3 text-sm font-semibold text-slate-200">
          Pipeline
        </h2>
        <PipelineDiagram
          agentRuns={agentRuns}
          jobIsTerminal={isTerminal(jobStatus)}
        />
      </section>

      <LogPanel logs={logs} truncated={logsTruncated} />
    </div>
  );
}
