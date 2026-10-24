import { describe, expect, it } from "vitest";
import type { WorkerSession } from "./api.ts";
import { deriveWorkerState, toActivity } from "./workers.ts";

function worker(overrides: Partial<WorkerSession>): WorkerSession {
  return {
    id: "conv",
    title: null,
    agent_name: "claude",
    status: "idle",
    project_id: "proj",
    created_at: 0,
    updated_at: 0,
    host_online: true,
    runner_online: true,
    pending_elicitations_count: 0,
    ...overrides,
  };
}

describe("deriveWorkerState", () => {
  it.each([
    [{ status: "running" }, "working"],
    [{ status: "running", pending_elicitations_count: 1 }, "waiting_human"],
    [{ status: "waiting" }, "waiting_dependency"],
    [{ status: "failed", host_online: false }, "failed"],
    [{ status: "idle" }, "completed"],
  ] as const)("maps %o to %s", (overrides, expected) => {
    expect(deriveWorkerState(worker(overrides))).toBe(expected);
  });

  it("never shows a lost host as progress", () => {
    expect(deriveWorkerState(worker({ status: "running", host_online: false }))).toBe("offline");
  });
});

describe("toActivity", () => {
  it("shows messages, tool use, and errors but never reasoning", () => {
    const activity = toActivity([
      { id: "1", type: "message", role: "user", content: [{ type: "input_text", text: "Fix the build" }] },
      { id: "2", type: "reasoning" },
      { id: "3", type: "function_call", name: "Bash" },
      { id: "4", type: "message", role: "assistant", content: [{ type: "output_text", text: "Done." }] },
      { id: "5", type: "message", role: "user", is_meta: true, content: [{ type: "input_text", text: "hidden" }] },
      { id: "6", type: "error", message: "Rate limited" },
    ]);
    expect(activity.map((entry) => [entry.kind, entry.text])).toEqual([
      ["you", "Fix the build"],
      ["tool", "Used tool Bash"],
      ["agent", "Done."],
      ["error", "Rate limited"],
    ]);
  });
});
