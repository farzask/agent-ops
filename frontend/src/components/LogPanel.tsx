"use client";

/**
 * Scrollable live log list with a per-agent filter (TECH_SPEC §7.2).
 *
 * Auto-scroll follows the tail but yields the moment the user scrolls up — an
 * auto-scroll that fights the user is worse than none.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import type { LogEntry } from "@/lib/api";
import { LOG_LEVEL_STYLES } from "@/lib/status";

const ALL_AGENTS = "__all__";
const JOB_LEVEL = "__job__";

/** Distinct agent names present in the run, derived — never hardcoded. */
export function agentOptions(logs: LogEntry[]): string[] {
  const names = new Set<string>();
  for (const entry of logs) {
    if (entry.agent_name) names.add(entry.agent_name);
  }
  return [...names].sort();
}

export function filterLogs(logs: LogEntry[], selected: string): LogEntry[] {
  if (selected === ALL_AGENTS) return logs;
  if (selected === JOB_LEVEL) return logs.filter((e) => !e.agent_name);
  return logs.filter((entry) => entry.agent_name === selected);
}

function formatTime(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleTimeString(undefined, {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

interface LogPanelProps {
  logs: LogEntry[];
  truncated?: boolean;
}

export function LogPanel({ logs, truncated = false }: LogPanelProps) {
  const [selected, setSelected] = useState<string>(ALL_AGENTS);
  const [following, setFollowing] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);

  const options = useMemo(() => agentOptions(logs), [logs]);
  const visible = useMemo(() => filterLogs(logs, selected), [logs, selected]);
  const hasJobLevel = useMemo(
    () => logs.some((entry) => !entry.agent_name),
    [logs],
  );

  useEffect(() => {
    if (!following) return;
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [visible.length, following]);

  const onScroll = (): void => {
    const node = scrollRef.current;
    if (!node) return;
    // 24px of slack so a near-bottom position still counts as following.
    const atBottom =
      node.scrollHeight - node.scrollTop - node.clientHeight < 24;
    setFollowing(atBottom);
  };

  return (
    <section className="flex flex-col rounded-lg border border-slate-800 bg-slate-950">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-slate-200">Live logs</h2>
          <span className="text-xs text-slate-500">
            {visible.length} line{visible.length === 1 ? "" : "s"}
          </span>
          {!following && (
            <button
              type="button"
              onClick={() => setFollowing(true)}
              className="rounded border border-blue-500/60 px-2 py-0.5 text-xs text-blue-300 hover:bg-blue-500/10"
            >
              Resume auto-scroll
            </button>
          )}
        </div>

        <label className="flex items-center gap-2 text-xs text-slate-400">
          Agent
          <select
            value={selected}
            onChange={(event) => setSelected(event.target.value)}
            aria-label="Filter logs by agent"
            className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-200"
          >
            <option value={ALL_AGENTS}>All</option>
            {hasJobLevel && <option value={JOB_LEVEL}>Pipeline (job-level)</option>}
            {options.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
      </header>

      {truncated && (
        <p className="border-b border-amber-500/30 bg-amber-500/10 px-4 py-1.5 text-xs text-amber-300">
          Older lines were dropped to cap memory use. The full history is
          available via the run detail API.
        </p>
      )}

      <div
        ref={scrollRef}
        onScroll={onScroll}
        data-testid="log-scroll"
        role="log"
        aria-live="polite"
        aria-label="Pipeline log output"
        className="h-80 overflow-y-auto px-4 py-2 font-mono text-xs leading-relaxed"
      >
        {visible.length === 0 ? (
          <p className="py-8 text-center text-slate-600">
            No log lines yet. Events stream in as the pipeline runs.
          </p>
        ) : (
          <ul className="space-y-0.5">
            {visible.map((entry) => {
              const level = LOG_LEVEL_STYLES[entry.level] ?? LOG_LEVEL_STYLES.info;
              return (
                // Keyed by the backend log id, never the array index — an index
                // key makes React mis-diff when the filter changes.
                <li key={entry.log_id} className="flex gap-2">
                  <span className="shrink-0 text-slate-600">
                    {formatTime(entry.timestamp)}
                  </span>
                  <span className={`w-12 shrink-0 font-semibold ${level.className}`}>
                    {level.label}
                  </span>
                  <span className="w-24 shrink-0 truncate text-violet-300">
                    {entry.agent_name ?? "pipeline"}
                  </span>
                  <span className="whitespace-pre-wrap break-words text-slate-300">
                    {entry.message}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}
