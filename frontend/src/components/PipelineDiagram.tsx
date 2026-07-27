"use client";

/**
 * Hand-rolled SVG pipeline diagram.
 *
 * TECH_SPEC §2 chooses hand-rolled SVG over a graph library because the v1
 * topology is fixed. Do not add reactflow / d3 / dagre here.
 */

import type { AgentRunDetail } from "@/lib/api";
import type { AgentStatus } from "@/lib/events";
import { agentStatusStyle, formatDuration } from "@/lib/status";

/** Placeholder nodes so the diagram is never empty on first paint. */
const PLACEHOLDER_NODES: ReadonlyArray<{ name: string; sequence: number }> = [
  { name: "Supervisor", sequence: 0 },
  { name: "Clarifier", sequence: 1 },
  { name: "Worker", sequence: 2 },
  { name: "Verifier", sequence: 3 },
];

const NODE_WIDTH = 132;
const NODE_HEIGHT = 62;
const GAP = 46;
const PADDING = 16;
const SVG_HEIGHT = NODE_HEIGHT + PADDING * 2 + 26;

export interface PipelineNode {
  key: string;
  label: string;
  status: AgentStatus;
  attemptCount: number;
  reworkCount: number;
  durationMs: number | null;
  failureReason: string | null;
}

/**
 * Build the node list from live agent runs, falling back to placeholders.
 *
 * Exported for tests: this is the status→node mapping TECH_SPEC §10 requires
 * coverage of.
 */
export function buildNodes(
  agentRuns: AgentRunDetail[],
  jobIsTerminal: boolean,
): PipelineNode[] {
  if (agentRuns.length === 0) {
    return PLACEHOLDER_NODES.map((node) => ({
      key: node.name,
      label: node.name,
      status: "idle" as AgentStatus,
      attemptCount: 0,
      reworkCount: 0,
      durationMs: null,
      failureReason: null,
    }));
  }

  const ordered = [...agentRuns].sort(
    (a, b) => a.sequence_index - b.sequence_index,
  );

  const nodes: PipelineNode[] = ordered.map((run) => ({
    key: run.agent_run_id || run.agent_name,
    label: run.agent_name,
    status: run.status,
    attemptCount: run.attempt_count,
    reworkCount: run.rework_count,
    durationMs: run.duration_ms,
    failureReason: run.failure_reason,
  }));

  // The terminal "Done" node from TECH_SPEC §3 is a pipeline state, not an
  // agent, so it has no agent_runs row — synthesise it.
  const allCompleted =
    ordered.length > 0 && ordered.every((run) => run.status === "completed");
  nodes.push({
    key: "__done__",
    label: "Done",
    status: jobIsTerminal && allCompleted ? "completed" : "idle",
    attemptCount: 0,
    reworkCount: 0,
    durationMs: null,
    failureReason: null,
  });

  return nodes;
}

interface PipelineDiagramProps {
  agentRuns: AgentRunDetail[];
  jobIsTerminal?: boolean;
}

export function PipelineDiagram({
  agentRuns,
  jobIsTerminal = false,
}: PipelineDiagramProps) {
  const nodes = buildNodes(agentRuns, jobIsTerminal);
  const width = PADDING * 2 + nodes.length * NODE_WIDTH + (nodes.length - 1) * GAP;

  return (
    <div className="overflow-x-auto">
      <svg
        width={width}
        height={SVG_HEIGHT}
        viewBox={`0 0 ${width} ${SVG_HEIGHT}`}
        role="group"
        aria-label="Agent pipeline"
        className="min-w-full"
      >
        <defs>
          <marker
            id="arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#475569" />
          </marker>
        </defs>

        {nodes.map((node, index) => {
          const style = agentStatusStyle(node.status);
          const x = PADDING + index * (NODE_WIDTH + GAP);
          const y = PADDING;
          const centerY = y + NODE_HEIGHT / 2;

          // Text label alongside colour: never encode state by colour alone.
          const label =
            node.attemptCount > 1
              ? `${style.label} · attempt ${node.attemptCount}`
              : style.label;

          return (
            <g key={node.key}>
              {index < nodes.length - 1 && (
                <line
                  x1={x + NODE_WIDTH}
                  y1={centerY}
                  x2={x + NODE_WIDTH + GAP - 4}
                  y2={centerY}
                  stroke="#475569"
                  strokeWidth={1.5}
                  markerEnd="url(#arrow)"
                />
              )}

              <g
                role="img"
                aria-label={`${node.label}: ${label}${
                  node.failureReason ? `. ${node.failureReason}` : ""
                }`}
                data-testid={`node-${node.label}`}
                data-status={node.status}
              >
                <rect
                  x={x}
                  y={y}
                  width={NODE_WIDTH}
                  height={NODE_HEIGHT}
                  rx={8}
                  fill={style.nodeFill}
                  stroke={style.nodeStroke}
                  strokeWidth={2}
                  className={
                    style.animated
                      ? // motion-safe: respects prefers-reduced-motion.
                        "motion-safe:animate-pulse"
                      : undefined
                  }
                />
                <text
                  x={x + NODE_WIDTH / 2}
                  y={y + 25}
                  textAnchor="middle"
                  fill={style.nodeText}
                  fontSize={13}
                  fontWeight={600}
                >
                  {node.label}
                </text>
                <text
                  x={x + NODE_WIDTH / 2}
                  y={y + 44}
                  textAnchor="middle"
                  fill={style.nodeText}
                  fontSize={10.5}
                  opacity={0.85}
                >
                  {label}
                </text>
              </g>

              {node.durationMs != null && (
                <text
                  x={x + NODE_WIDTH / 2}
                  y={y + NODE_HEIGHT + 18}
                  textAnchor="middle"
                  fill="#64748b"
                  fontSize={10}
                >
                  {formatDuration(node.durationMs)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
