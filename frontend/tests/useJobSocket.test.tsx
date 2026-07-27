/**
 * useJobSocket: reconnect logic against a mock WebSocket (TECH_SPEC §10),
 * plus the merge/dedup helpers that make backfill safe (TECH_SPEC §6).
 */

import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  MAX_LOG_LINES,
  applyAgentStatus,
  mergeLogs,
  reconnectDelay,
  useJobSocket,
} from "@/hooks/useJobSocket";
import type { AgentRunDetail, JobDetail, LogEntry } from "@/lib/api";
import type { AgentStatus } from "@/lib/events";

// ---------------------------------------------------------------------------
// Mock WebSocket
// ---------------------------------------------------------------------------

class MockSocket {
  static instances: MockSocket[] = [];

  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    MockSocket.instances.push(this);
  }

  open(): void {
    this.onopen?.();
  }

  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }

  emitRaw(data: unknown): void {
    this.onmessage?.({ data } as MessageEvent);
  }

  /** Simulate the server or network dropping the connection. */
  drop(): void {
    this.closed = true;
    this.onclose?.();
  }

  close(): void {
    this.closed = true;
  }

  static reset(): void {
    MockSocket.instances = [];
  }

  static get latest(): MockSocket {
    const socket = MockSocket.instances.at(-1);
    if (!socket) throw new Error("no socket was created");
    return socket;
  }
}

function job(overrides: Partial<JobDetail> = {}): JobDetail {
  return {
    job_id: "job-1",
    status: "running",
    task_description: "write a post",
    created_at: new Date(0).toISOString(),
    started_at: new Date(0).toISOString(),
    completed_at: null,
    duration_ms: null,
    final_output: null,
    failure_reason: null,
    agent_runs: [],
    ...overrides,
  };
}

/** Minimal harness that surfaces hook state as DOM text. */
function Harness({
  initialJob,
  initialLogs = [],
}: {
  initialJob: JobDetail;
  initialLogs?: LogEntry[];
}) {
  const state = useJobSocket({
    initialJob,
    initialLogs,
    socketFactory: (url) => new MockSocket(url) as unknown as WebSocket,
  });
  return (
    <div>
      <span data-testid="connection">{state.connection}</span>
      <span data-testid="job-status">{state.jobStatus}</span>
      <span data-testid="log-count">{state.logs.length}</span>
      <span data-testid="agent-count">{state.agentRuns.length}</span>
      <span data-testid="last-log">{state.logs.at(-1)?.message ?? ""}</span>
      <span data-testid="agent-statuses">
        {state.agentRuns.map((r) => `${r.agent_name}=${r.status}`).join(",")}
      </span>
    </div>
  );
}

beforeEach(() => {
  MockSocket.reset();
  vi.useFakeTimers({ shouldAdvanceTime: true });
  // The hook backfills over fetch after a reconnect; keep it inert and
  // non-throwing so we are testing reconnect behaviour, not fetch behaviour.
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ logs: [], next_since: null, ...job() }),
      } as Response),
    ),
  );
});

afterEach(() => {
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// Backoff schedule
// ---------------------------------------------------------------------------

describe("reconnectDelay", () => {
  it("grows exponentially", () => {
    // Jitter is 50-100% of the capped value, so compare ranges not equality.
    const a = reconnectDelay(0);
    const b = reconnectDelay(3);
    expect(a).toBeGreaterThanOrEqual(500);
    expect(a).toBeLessThanOrEqual(1000);
    expect(b).toBeGreaterThanOrEqual(4000);
    expect(b).toBeLessThanOrEqual(8000);
  });

  it("caps the delay so retries never drift into minutes", () => {
    for (const attempt of [10, 20, 50]) {
      expect(reconnectDelay(attempt)).toBeLessThanOrEqual(30_000);
    }
  });

  it("always applies jitter, so many tabs do not reconnect in lockstep", () => {
    const samples = new Set(Array.from({ length: 30 }, () => reconnectDelay(5)));
    expect(samples.size).toBeGreaterThan(1);
  });
});

// ---------------------------------------------------------------------------
// Connection lifecycle
// ---------------------------------------------------------------------------

describe("connection lifecycle", () => {
  it("connects to the per-job endpoint and reports open", async () => {
    render(<Harness initialJob={job()} />);

    expect(MockSocket.instances).toHaveLength(1);
    expect(MockSocket.latest.url).toContain("/ws/jobs/job-1");

    await act(async () => {
      MockSocket.latest.open();
    });
    expect(screen.getByTestId("connection")).toHaveTextContent("open");
  });

  it("reconnects after an unexpected drop", async () => {
    render(<Harness initialJob={job()} />);
    await act(async () => {
      MockSocket.latest.open();
    });

    await act(async () => {
      MockSocket.latest.drop();
    });
    expect(screen.getByTestId("connection")).toHaveTextContent("reconnecting");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });

    expect(MockSocket.instances).toHaveLength(2);
  });

  it("backs off further on each successive failure", async () => {
    render(<Harness initialJob={job()} />);
    await act(async () => {
      MockSocket.latest.open();
    });

    for (let i = 0; i < 3; i += 1) {
      await act(async () => {
        MockSocket.latest.drop();
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(35_000);
      });
    }

    // One initial + three reconnects.
    expect(MockSocket.instances).toHaveLength(4);
  });

  it("does not reconnect once the job reaches a terminal status", async () => {
    render(<Harness initialJob={job()} />);
    await act(async () => {
      MockSocket.latest.open();
    });

    await act(async () => {
      MockSocket.latest.emit({
        event_type: "job_status_changed",
        job_id: "job-1",
        timestamp: new Date(1000).toISOString(),
        payload: { previous_status: "running", new_status: "completed" },
      });
    });

    await act(async () => {
      MockSocket.latest.drop();
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(screen.getByTestId("job-status")).toHaveTextContent("completed");
    // Retrying a channel the server closed would look broken in the network tab.
    expect(MockSocket.instances).toHaveLength(1);
    expect(screen.getByTestId("connection")).toHaveTextContent("closed");
  });

  it("never opens a socket for an already-finished run", async () => {
    render(<Harness initialJob={job({ status: "completed" })} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(MockSocket.instances).toHaveLength(0);
  });

  it("closes the socket and cancels timers on unmount", async () => {
    const { unmount } = render(<Harness initialJob={job()} />);
    await act(async () => {
      MockSocket.latest.open();
    });
    const socket = MockSocket.latest;

    unmount();

    expect(socket.closed).toBe(true);
    // Handlers are detached so a late onclose cannot schedule a reconnect.
    expect(socket.onclose).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(MockSocket.instances).toHaveLength(1);
  });

  it("backfills on reconnect but not on the first connect", async () => {
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    render(<Harness initialJob={job()} />);

    await act(async () => {
      MockSocket.latest.open();
    });
    // First connect is already seeded by server-rendered state.
    expect(fetchMock).not.toHaveBeenCalled();

    await act(async () => {
      MockSocket.latest.drop();
      await vi.advanceTimersByTimeAsync(2_000);
    });
    await act(async () => {
      MockSocket.latest.open();
    });

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  });
});

// ---------------------------------------------------------------------------
// Event handling
// ---------------------------------------------------------------------------

describe("event handling", () => {
  it("appends log lines from the stream", async () => {
    render(<Harness initialJob={job()} />);
    await act(async () => {
      MockSocket.latest.open();
      MockSocket.latest.emit({
        event_type: "log_line",
        job_id: "job-1",
        timestamp: new Date(1000).toISOString(),
        payload: {
          log_id: "log-1",
          agent_name: "Supervisor",
          level: "info",
          message: "Decomposed task into 3 subtasks",
        },
      });
    });

    expect(screen.getByTestId("log-count")).toHaveTextContent("1");
    expect(screen.getByTestId("last-log")).toHaveTextContent(
      "Decomposed task into 3 subtasks",
    );
  });

  it("upserts agent status by name rather than adding a node per event", async () => {
    render(<Harness initialJob={job()} />);
    await act(async () => {
      MockSocket.latest.open();
    });

    for (const status of ["queued", "running", "completed"] as AgentStatus[]) {
      await act(async () => {
        MockSocket.latest.emit({
          event_type: "agent_status_changed",
          job_id: "job-1",
          timestamp: new Date(1000).toISOString(),
          payload: {
            agent_run_id: "run-1",
            agent_name: "Supervisor",
            sequence_index: 0,
            previous_status: null,
            new_status: status,
            attempt_count: 1,
            rework_count: 0,
            failure_reason: null,
          },
        });
      });
    }

    expect(screen.getByTestId("agent-count")).toHaveTextContent("1");
    expect(screen.getByTestId("agent-statuses")).toHaveTextContent(
      "Supervisor=completed",
    );
  });

  it("ignores events for a different job", async () => {
    render(<Harness initialJob={job()} />);
    await act(async () => {
      MockSocket.latest.open();
      MockSocket.latest.emit({
        event_type: "log_line",
        job_id: "some-other-job",
        timestamp: new Date(1000).toISOString(),
        payload: { log_id: "x", agent_name: null, level: "info", message: "nope" },
      });
    });
    expect(screen.getByTestId("log-count")).toHaveTextContent("0");
  });

  it("drops malformed frames instead of putting undefined into state", async () => {
    render(<Harness initialJob={job()} />);
    await act(async () => {
      MockSocket.latest.open();
      MockSocket.latest.emitRaw("not json at all");
      MockSocket.latest.emitRaw("{}");
      MockSocket.latest.emit({ event_type: "unknown_kind", job_id: "job-1" });
      MockSocket.latest.emitRaw(new ArrayBuffer(4));
    });
    expect(screen.getByTestId("log-count")).toHaveTextContent("0");
    expect(screen.getByTestId("connection")).toHaveTextContent("open");
  });
});

// ---------------------------------------------------------------------------
// Merge / dedup helpers
// ---------------------------------------------------------------------------

function entry(id: string, at: number, message = id): LogEntry {
  return {
    log_id: id,
    agent_name: "Supervisor",
    timestamp: new Date(at).toISOString(),
    level: "info",
    message,
  };
}

describe("mergeLogs", () => {
  it("drops duplicates, which backfill and the live stream produce by design", () => {
    const existing = [entry("a", 1000), entry("b", 2000)];
    const merged = mergeLogs(existing, [entry("b", 2000), entry("c", 3000)]);
    expect(merged.map((e) => e.log_id)).toEqual(["a", "b", "c"]);
  });

  it("sorts by timestamp so out-of-order backfill lands correctly", () => {
    const merged = mergeLogs([entry("c", 3000)], [entry("a", 1000), entry("b", 2000)]);
    expect(merged.map((e) => e.log_id)).toEqual(["a", "b", "c"]);
  });

  it("breaks timestamp ties deterministically", () => {
    const first = mergeLogs([], [entry("b", 1000), entry("a", 1000)]);
    const second = mergeLogs([], [entry("a", 1000), entry("b", 1000)]);
    expect(first.map((e) => e.log_id)).toEqual(second.map((e) => e.log_id));
  });

  it("returns the original array when there is nothing to add", () => {
    const existing = [entry("a", 1000)];
    expect(mergeLogs(existing, [])).toBe(existing);
  });
});

describe("log cap", () => {
  it("keeps only the newest MAX_LOG_LINES and flags truncation", async () => {
    const many = Array.from({ length: MAX_LOG_LINES + 50 }, (_, i) =>
      entry(`log-${i}`, 1000 + i),
    );
    render(<Harness initialJob={job()} initialLogs={many.slice(0, MAX_LOG_LINES)} />);

    await act(async () => {
      MockSocket.latest.open();
      for (const extra of many.slice(MAX_LOG_LINES)) {
        MockSocket.latest.emit({
          event_type: "log_line",
          job_id: "job-1",
          timestamp: extra.timestamp,
          payload: {
            log_id: extra.log_id,
            agent_name: extra.agent_name,
            level: extra.level,
            message: extra.message,
          },
        });
      }
    });

    expect(screen.getByTestId("log-count")).toHaveTextContent(String(MAX_LOG_LINES));
    expect(screen.getByTestId("last-log")).toHaveTextContent(
      `log-${MAX_LOG_LINES + 49}`,
    );
  });
});

describe("applyAgentStatus", () => {
  function existing(status: AgentStatus): AgentRunDetail[] {
    return [
      {
        agent_run_id: "run-1",
        agent_name: "Supervisor",
        sequence_index: 0,
        status,
        attempt_count: 1,
        rework_count: 0,
        started_at: null,
        completed_at: null,
        duration_ms: null,
        output_payload: null,
        failure_reason: null,
      },
    ];
  }

  const payload = {
    agent_run_id: "run-1",
    agent_name: "Supervisor",
    sequence_index: 0,
    new_status: "retrying" as AgentStatus,
    attempt_count: 2,
    rework_count: 0,
    failure_reason: "timeout",
  };

  it("updates in place and preserves fields the event does not carry", () => {
    const runs = applyAgentStatus(existing("running"), payload);
    expect(runs).toHaveLength(1);
    expect(runs[0]).toMatchObject({
      status: "retrying",
      attempt_count: 2,
      failure_reason: "timeout",
      agent_run_id: "run-1",
    });
  });

  it("inserts an unseen agent in sequence order", () => {
    const runs = applyAgentStatus(existing("completed"), {
      ...payload,
      agent_run_id: "run-3",
      agent_name: "Worker-1",
      sequence_index: 2,
      new_status: "running",
    });
    expect(runs.map((r) => r.agent_name)).toEqual(["Supervisor", "Worker-1"]);
  });

  it("keeps sequence order when an event arrives out of order", () => {
    const runs = applyAgentStatus(existing("completed"), {
      ...payload,
      agent_run_id: "run-0",
      agent_name: "Preflight",
      sequence_index: -1,
      new_status: "completed",
    });
    expect(runs.map((r) => r.agent_name)).toEqual(["Preflight", "Supervisor"]);
  });

  it("does not mutate the input array", () => {
    const input = existing("running");
    const snapshot = input[0].status;
    applyAgentStatus(input, payload);
    expect(input[0].status).toBe(snapshot);
  });
});
