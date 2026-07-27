/**
 * PipelineDiagram status→colour mapping (TECH_SPEC §10, PRD §7.2).
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PipelineDiagram, buildNodes } from "@/components/PipelineDiagram";
import type { AgentRunDetail } from "@/lib/api";
import type { AgentStatus } from "@/lib/events";
import { AGENT_STATUS_STYLES } from "@/lib/status";

function run(
  name: string,
  index: number,
  status: AgentStatus,
  extra: Partial<AgentRunDetail> = {},
): AgentRunDetail {
  return {
    agent_run_id: `id-${name}`,
    agent_name: name,
    sequence_index: index,
    status,
    attempt_count: 1,
    rework_count: 0,
    started_at: null,
    completed_at: null,
    duration_ms: null,
    output_payload: null,
    failure_reason: null,
    ...extra,
  };
}

describe("status → colour mapping", () => {
  it("defines a distinct colour for every agent status", () => {
    const statuses: AgentStatus[] = [
      "idle",
      "queued",
      "running",
      "completed",
      "failed",
      "retrying",
    ];
    const fills = statuses.map((s) => AGENT_STATUS_STYLES[s].nodeFill);
    expect(new Set(fills).size).toBe(statuses.length);
  });

  it("animates only running and retrying (PRD §7.2)", () => {
    expect(AGENT_STATUS_STYLES.running.animated).toBe(true);
    expect(AGENT_STATUS_STYLES.retrying.animated).toBe(true);
    expect(AGENT_STATUS_STYLES.idle.animated).toBe(false);
    expect(AGENT_STATUS_STYLES.queued.animated).toBe(false);
    expect(AGENT_STATUS_STYLES.completed.animated).toBe(false);
    expect(AGENT_STATUS_STYLES.failed.animated).toBe(false);
  });

  it("gives every status a text label, so colour is never the only signal", () => {
    for (const style of Object.values(AGENT_STATUS_STYLES)) {
      expect(style.label.length).toBeGreaterThan(0);
    }
  });

  it("renders each node with the colour its status maps to", () => {
    render(
      <PipelineDiagram
        agentRuns={[
          run("Supervisor", 0, "completed"),
          run("Clarifier", 1, "running"),
          run("Worker-1", 2, "failed"),
          run("Verifier", 3, "idle"),
        ]}
      />,
    );

    const cases: Array<[string, AgentStatus]> = [
      ["Supervisor", "completed"],
      ["Clarifier", "running"],
      ["Worker-1", "failed"],
      ["Verifier", "idle"],
    ];

    for (const [name, status] of cases) {
      const node = screen.getByTestId(`node-${name}`);
      expect(node).toHaveAttribute("data-status", status);
      const rect = node.querySelector("rect");
      expect(rect).toHaveAttribute("fill", AGENT_STATUS_STYLES[status].nodeFill);
    }
  });

  it("applies a motion-safe pulse to running nodes only", () => {
    render(
      <PipelineDiagram
        agentRuns={[run("Supervisor", 0, "running"), run("Clarifier", 1, "completed")]}
      />,
    );

    const running = screen.getByTestId("node-Supervisor").querySelector("rect");
    const done = screen.getByTestId("node-Clarifier").querySelector("rect");

    // motion-safe: honours prefers-reduced-motion.
    expect(running?.getAttribute("class")).toContain("motion-safe:animate-pulse");
    expect(done?.getAttribute("class") ?? "").not.toContain("animate-pulse");
  });
});

describe("accessibility", () => {
  it("labels each node with its agent name and status", () => {
    render(<PipelineDiagram agentRuns={[run("Worker-1", 2, "running")]} />);
    expect(screen.getByLabelText(/Worker-1: Running/)).toBeInTheDocument();
  });

  it("includes the failure reason in the accessible label", () => {
    render(
      <PipelineDiagram
        agentRuns={[
          run("Worker-1", 2, "failed", { failure_reason: "retries exhausted" }),
        ]}
      />,
    );
    expect(
      screen.getByLabelText(/Worker-1: Failed\. retries exhausted/),
    ).toBeInTheDocument();
  });

  it("surfaces the attempt count when an agent has retried", () => {
    render(
      <PipelineDiagram
        agentRuns={[run("Supervisor", 0, "running", { attempt_count: 3 })]}
      />,
    );
    expect(screen.getByLabelText(/attempt 3/)).toBeInTheDocument();
  });
});

describe("buildNodes", () => {
  it("shows placeholder idle nodes before any agent run exists", () => {
    const nodes = buildNodes([], false);
    expect(nodes.map((n) => n.label)).toEqual([
      "Supervisor",
      "Clarifier",
      "Worker",
      "Verifier",
    ]);
    expect(nodes.every((n) => n.status === "idle")).toBe(true);
  });

  it("orders nodes by sequence_index regardless of input order", () => {
    const nodes = buildNodes(
      [
        run("Verifier", 4, "idle"),
        run("Supervisor", 0, "completed"),
        run("Worker-1", 2, "running"),
        run("Clarifier", 1, "completed"),
      ],
      false,
    );
    expect(nodes.map((n) => n.label)).toEqual([
      "Supervisor",
      "Clarifier",
      "Worker-1",
      "Verifier",
      "Done",
    ]);
  });

  it("appends a Done node that only completes when the run finished cleanly", () => {
    const running = buildNodes([run("Supervisor", 0, "running")], false);
    expect(running.at(-1)).toMatchObject({ label: "Done", status: "idle" });

    const finished = buildNodes([run("Supervisor", 0, "completed")], true);
    expect(finished.at(-1)).toMatchObject({ label: "Done", status: "completed" });
  });

  it("leaves Done idle when the run failed, even though it is terminal", () => {
    const nodes = buildNodes(
      [run("Supervisor", 0, "completed"), run("Worker-1", 2, "failed")],
      true,
    );
    expect(nodes.at(-1)).toMatchObject({ label: "Done", status: "idle" });
  });
});
