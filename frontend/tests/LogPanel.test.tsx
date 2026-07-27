/**
 * LogPanel append behaviour and filtering (TECH_SPEC §10).
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { LogPanel, agentOptions, filterLogs } from "@/components/LogPanel";
import type { LogEntry } from "@/lib/api";
import type { LogLevel } from "@/lib/events";

let clock = 0;

function log(
  message: string,
  agent: string | null = "Supervisor",
  level: LogLevel = "info",
): LogEntry {
  clock += 1000;
  return {
    log_id: `log-${clock}`,
    agent_name: agent,
    timestamp: new Date(clock).toISOString(),
    level,
    message,
  };
}

describe("append behaviour", () => {
  it("renders new lines when the log list grows", () => {
    const first = [log("Pipeline started", null)];
    const { rerender } = render(<LogPanel logs={first} />);

    expect(screen.getByText("Pipeline started")).toBeInTheDocument();
    expect(screen.queryByText("Decomposed task into 3 subtasks")).toBeNull();

    rerender(
      <LogPanel logs={[...first, log("Decomposed task into 3 subtasks")]} />,
    );

    expect(screen.getByText("Pipeline started")).toBeInTheDocument();
    expect(
      screen.getByText("Decomposed task into 3 subtasks"),
    ).toBeInTheDocument();
  });

  it("preserves order as lines are appended", () => {
    const logs = [log("first", null), log("second"), log("third")];
    render(<LogPanel logs={logs} />);

    const items = screen.getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual([
      expect.stringContaining("first"),
      expect.stringContaining("second"),
      expect.stringContaining("third"),
    ]);
  });

  it("shows a live line count that tracks appends", () => {
    const logs = [log("one"), log("two")];
    const { rerender } = render(<LogPanel logs={logs} />);
    expect(screen.getByText("2 lines")).toBeInTheDocument();

    rerender(<LogPanel logs={[...logs, log("three")]} />);
    expect(screen.getByText("3 lines")).toBeInTheDocument();
  });

  it("shows an empty state rather than a bare panel", () => {
    render(<LogPanel logs={[]} />);
    expect(screen.getByText(/No log lines yet/)).toBeInTheDocument();
  });

  it("warns when older lines were dropped", () => {
    render(<LogPanel logs={[log("x")]} truncated />);
    expect(screen.getByText(/Older lines were dropped/)).toBeInTheDocument();
  });
});

describe("level styling", () => {
  it("labels each level distinctly", () => {
    render(
      <LogPanel
        logs={[
          log("info line", "Supervisor", "info"),
          log("warn line", "Clarifier", "warn"),
          log("error line", "Worker-1", "error"),
        ]}
      />,
    );
    expect(screen.getByText("INFO")).toBeInTheDocument();
    expect(screen.getByText("WARN")).toBeInTheDocument();
    expect(screen.getByText("ERROR")).toBeInTheDocument();
  });
});

describe("per-agent filter", () => {
  it("derives options from the agents present, not a hardcoded list", () => {
    const logs = [
      log("a", "Supervisor"),
      log("b", "Worker-1"),
      log("c", "Worker-1"),
      log("d", null),
    ];
    expect(agentOptions(logs)).toEqual(["Supervisor", "Worker-1"]);
  });

  it("filters to a single agent", () => {
    const logs = [log("a", "Supervisor"), log("b", "Worker-1")];
    const filtered = filterLogs(logs, "Worker-1");
    expect(filtered).toHaveLength(1);
    expect(filtered[0].message).toBe("b");
  });

  it("filters job-level lines separately from agent lines", () => {
    const logs = [log("agent line", "Supervisor"), log("job line", null)];
    const filtered = filterLogs(logs, "__job__");
    expect(filtered.map((e) => e.message)).toEqual(["job line"]);
  });

  it("returns everything for the all-agents selection", () => {
    const logs = [log("a", "Supervisor"), log("b", null)];
    expect(filterLogs(logs, "__all__")).toHaveLength(2);
  });

  it("hides other agents' lines when a filter is applied in the UI", async () => {
    const user = userEvent.setup();
    render(
      <LogPanel
        logs={[log("supervisor says hi", "Supervisor"), log("worker says hi", "Worker-1")]}
      />,
    );

    await user.selectOptions(
      screen.getByLabelText("Filter logs by agent"),
      "Worker-1",
    );

    expect(screen.getByText("worker says hi")).toBeInTheDocument();
    expect(screen.queryByText("supervisor says hi")).toBeNull();
  });
});

describe("keys", () => {
  it("renders one row per log id even when messages repeat", () => {
    // Duplicate text with distinct ids would collide under an index key.
    const logs = [log("same message"), log("same message"), log("same message")];
    render(<LogPanel logs={logs} />);
    expect(screen.getAllByText("same message")).toHaveLength(3);
  });
});
