/**
 * The WS event contract (TECH_SPEC §6).
 *
 * These tests guard the boundary where untrusted socket data becomes typed
 * application state.
 */

import { describe, expect, it } from "vitest";

import { assertNever, eventKey, parseEvent } from "@/lib/events";
import { formatDuration, isTerminal } from "@/lib/status";

const validLogLine = {
  event_type: "log_line",
  job_id: "job-1",
  timestamp: "2026-07-26T12:00:00Z",
  payload: {
    log_id: "log-1",
    agent_name: "Supervisor",
    level: "info",
    message: "Decomposed task into 3 subtasks",
  },
};

describe("parseEvent", () => {
  it("accepts each of the three event types in TECH_SPEC §6", () => {
    const types = [
      validLogLine,
      {
        event_type: "agent_status_changed",
        job_id: "job-1",
        timestamp: "2026-07-26T12:00:00Z",
        payload: { agent_name: "Worker-1", sequence_index: 2, new_status: "completed" },
      },
      {
        event_type: "job_status_changed",
        job_id: "job-1",
        timestamp: "2026-07-26T12:00:00Z",
        payload: { previous_status: "running", new_status: "completed" },
      },
    ];
    for (const event of types) {
      expect(parseEvent(JSON.stringify(event))).not.toBeNull();
    }
  });

  it.each([
    ["invalid JSON", "not json"],
    ["empty string", ""],
    ["a JSON array", "[1,2,3]"],
    ["a bare string", '"hello"'],
    ["null", "null"],
    ["an unknown event type", JSON.stringify({ ...validLogLine, event_type: "nope" })],
    ["a missing event type", JSON.stringify({ job_id: "j", timestamp: "t", payload: {} })],
    ["a missing job id", JSON.stringify({ ...validLogLine, job_id: undefined })],
    ["a non-string job id", JSON.stringify({ ...validLogLine, job_id: 42 })],
    ["a missing timestamp", JSON.stringify({ ...validLogLine, timestamp: undefined })],
    ["a null payload", JSON.stringify({ ...validLogLine, payload: null })],
    ["a non-object payload", JSON.stringify({ ...validLogLine, payload: "text" })],
  ])("rejects %s", (_label, raw) => {
    expect(parseEvent(raw)).toBeNull();
  });
});

describe("eventKey", () => {
  it("uses the backend log id when present, so dedup is exact", () => {
    const event = parseEvent(JSON.stringify(validLogLine));
    expect(event).not.toBeNull();
    expect(eventKey(event!)).toBe("log:log-1");
  });

  it("distinguishes two status changes for the same agent", () => {
    const base = {
      event_type: "agent_status_changed",
      job_id: "job-1",
      payload: {
        agent_name: "Worker-1",
        sequence_index: 2,
        new_status: "running",
      },
    };
    const first = parseEvent(
      JSON.stringify({ ...base, timestamp: "2026-07-26T12:00:00Z" }),
    );
    const second = parseEvent(
      JSON.stringify({
        ...base,
        timestamp: "2026-07-26T12:00:01Z",
        payload: { ...base.payload, new_status: "completed" },
      }),
    );
    expect(eventKey(first!)).not.toBe(eventKey(second!));
  });
});

describe("assertNever", () => {
  it("throws, so an unhandled event type surfaces at runtime too", () => {
    expect(() => assertNever("surprise" as never)).toThrow(/unhandled event type/);
  });
});

describe("isTerminal", () => {
  it("treats only completed and failed as terminal", () => {
    expect(isTerminal("completed")).toBe(true);
    expect(isTerminal("failed")).toBe(true);
    expect(isTerminal("running")).toBe(false);
    expect(isTerminal("queued")).toBe(false);
    expect(isTerminal(null)).toBe(false);
    expect(isTerminal(undefined)).toBe(false);
  });
});

describe("formatDuration", () => {
  it.each([
    [null, "—"],
    [undefined, "—"],
    [0, "0ms"],
    [450, "450ms"],
    [8213, "8.2s"],
    [59_999, "60.0s"],
    [65_000, "1m 5s"],
    [3_600_000, "60m 0s"],
  ])("formats %s as %s", (input, expected) => {
    expect(formatDuration(input as number | null | undefined)).toBe(expected);
  });
});
