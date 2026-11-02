import { describe, expect, it } from "vitest";
import type { WorkerSession } from "./api.ts";
import type { RoleHealth } from "../shared/troubleshoot.ts";
import { toActivity } from "../shared/sessionActivity.ts";
import { deriveWorkerState, roomWorkerState } from "./workers.ts";

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
    [{ status: "idle" }, "idle"],
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

describe("roomWorkerState", () => {
  const health = (overrides: Partial<RoleHealth>): RoleHealth => ({
    role: "pm",
    sessionId: "conv",
    temporary: false,
    harness: "codex",
    model: null,
    effort: null,
    harnessReadiness: "ready",
    state: "idle",
    problem: null,
    dismissed: false,
    scheduledRetry: null,
    ...overrides,
  });
  const problem = { title: "The turn stalled" } as RoleHealth["problem"];

  it("follows Omnigent's status when the office found no problem", () => {
    expect(roomWorkerState(worker({ status: "running" }), health({ state: "working" }))).toBe("working");
    expect(roomWorkerState(worker({ status: "idle" }), undefined)).toBe("idle");
  });

  it("shows a diagnosed problem as needing help unless it is dismissed, failed, or offline", () => {
    expect(roomWorkerState(worker({ status: "running" }), health({ state: "problem", problem }))).toBe("needs_help");
    expect(roomWorkerState(worker({ status: "idle" }), health({ state: "problem", problem, dismissed: true }))).toBe("idle");
    expect(roomWorkerState(worker({ status: "failed" }), health({ state: "problem", problem }))).toBe("failed");
    expect(roomWorkerState(worker({ host_online: false }), health({ state: "problem", problem }))).toBe("offline");
  });
});
