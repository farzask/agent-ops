"use client";

/**
 * Job Queue View. Subscribes to the global WS endpoint for live status badges
 * (TECH_SPEC §6) so the list updates without polling.
 */

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { fetchJobs, wsBaseUrl, type JobSummary } from "@/lib/api";
import { parseEvent } from "@/lib/events";
import { formatDuration, jobStatusStyle } from "@/lib/status";

interface JobQueueListProps {
  initialJobs: JobSummary[];
  initialTotal: number;
  /** Injectable for tests. */
  socketFactory?: (url: string) => WebSocket;
}

export function JobQueueList({
  initialJobs,
  initialTotal,
  socketFactory,
}: JobQueueListProps) {
  const [jobs, setJobs] = useState<JobSummary[]>(initialJobs);
  const [total, setTotal] = useState(initialTotal);
  const mountedRef = useRef(true);

  // Held in a ref, not a dependency: an inline factory from a caller would
  // otherwise reopen the socket on every render.
  const socketFactoryRef = useRef(socketFactory);
  socketFactoryRef.current = socketFactory;

  useEffect(() => {
    mountedRef.current = true;
    const factory =
      socketFactoryRef.current ??
      ((url: string): WebSocket => new WebSocket(url));

    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;

    try {
      socket = factory(`${wsBaseUrl()}/ws/jobs`);
    } catch {
      // No live feed available; the server-rendered list still shows.
      return;
    }

    socket.onmessage = (message: MessageEvent) => {
      if (!mountedRef.current || typeof message.data !== "string") return;
      const event = parseEvent(message.data);
      if (!event || event.event_type !== "job_status_changed") return;

      const { job_id: jobId, payload } = event;

      setJobs((current) => {
        const index = current.findIndex((job) => job.job_id === jobId);
        if (index === -1) {
          // A job submitted in another tab. Refetch rather than synthesise a
          // row from a partial event payload.
          if (timer === null) {
            timer = setTimeout(() => {
              timer = null;
              void fetchJobs({ limit: 25 })
                .then((page) => {
                  if (!mountedRef.current) return;
                  setJobs(page.jobs);
                  setTotal(page.total);
                })
                .catch(() => {
                  /* keep the current list */
                });
            }, 250);
          }
          return current;
        }

        const next = [...current];
        next[index] = {
          ...next[index],
          status: payload.new_status,
          duration_ms: payload.duration_ms ?? next[index].duration_ms,
        };
        return next;
      });
    };

    return () => {
      mountedRef.current = false;
      if (timer !== null) clearTimeout(timer);
      if (socket) {
        socket.onmessage = null;
        socket.close();
      }
    };
    // Mount-once: the global status feed is not scoped to any prop.
  }, []);

  return (
    <section className="rounded-lg border border-slate-800 bg-slate-950">
      <header className="flex items-center justify-between border-b border-slate-800 px-4 py-2.5">
        <h2 className="text-sm font-semibold text-slate-200">Job queue</h2>
        <span className="text-xs text-slate-500">
          {jobs.length} of {total}
        </span>
      </header>

      {jobs.length === 0 ? (
        <p className="px-4 py-10 text-center text-sm text-slate-600">
          No runs yet. Submit a task above to start the pipeline.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-slate-500">
              <tr>
                <th scope="col" className="px-4 py-2 font-medium">Task</th>
                <th scope="col" className="px-4 py-2 font-medium">Status</th>
                <th scope="col" className="px-4 py-2 font-medium">Submitted</th>
                <th scope="col" className="px-4 py-2 font-medium">Duration</th>
                <th scope="col" className="px-4 py-2 font-medium">
                  <span className="sr-only">Open</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/70">
              {jobs.map((job) => {
                const style = jobStatusStyle(job.status);
                return (
                  <tr key={job.job_id} className="hover:bg-slate-900/60">
                    <td className="max-w-md truncate px-4 py-2 text-slate-200">
                      {job.task_description}
                    </td>
                    <td className="px-4 py-2">
                      <span
                        className={`rounded px-2 py-0.5 text-[11px] ${style.badge}`}
                        data-testid={`status-${job.job_id}`}
                      >
                        {style.label}
                      </span>
                    </td>
                    <td className="px-4 py-2 font-mono text-slate-400">
                      {new Date(job.created_at).toLocaleString()}
                    </td>
                    <td className="px-4 py-2 font-mono text-slate-400">
                      {formatDuration(job.duration_ms)}
                    </td>
                    <td className="px-4 py-2">
                      <Link
                        href={`/jobs/${job.job_id}`}
                        className="text-blue-400 hover:text-blue-300 hover:underline"
                      >
                        View
                      </Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
