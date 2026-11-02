import { describe, expect, it } from "vitest";
import { type RoleWorkInput, roleWorkLine } from "./roleWork.ts";

const input = (overrides: Partial<RoleWorkInput>): RoleWorkInput => ({
  role: "developer",
  sessionId: "conv_1",
  busy: false,
  todos: [],
  items: [],
  tasks: [],
  ...overrides,
});
const said = (id: string, text: string, created_at = 10) => ({
  id,
  type: "message",
  role: "assistant",
  created_at,
  content: [{ type: "output_text", text }],
});

describe("roleWorkLine", () => {
  it("reports live work first: the in-progress todo, then the latest tool or message", () => {
    const todos = [
      { content: "Write tests", status: "completed" },
      { content: "Fix login", status: "in_progress", activeForm: "Fixing login" },
    ];
    expect(roleWorkLine(input({ busy: true, todos }))).toMatchObject({ kind: "todo", text: "Fixing login" });
    expect(roleWorkLine(input({ busy: true, items: [{ id: "2", type: "function_call", name: "Bash", created_at: 12 }, said("1", "Looking")] }))).toMatchObject({
      kind: "tool",
      text: "Using Bash",
      at: 12,
    });
    expect(roleWorkLine(input({ busy: true }))).toMatchObject({ kind: "task", text: "Working" });
  });

  it("between turns, prefers the most pressing open task over the last message", () => {
    const tasks = [
      { title: "Review copy", status: "submitted" as const },
      { title: "Ship checkout", status: "blocked" as const },
    ];
    expect(roleWorkLine(input({ tasks, items: [said("1", "Done for now.")] }))).toMatchObject({ kind: "task", text: "Blocked on: Ship checkout" });
    expect(roleWorkLine(input({ items: [said("1", "All   tests\npass.")] }))).toMatchObject({ kind: "message", text: "Last said: All tests pass." });
    expect(roleWorkLine(input({}))).toMatchObject({ kind: "idle", text: "Idle, no open tasks" });
  });

  it("surfaces a turn error, and ignores the PO's and relayed messages", () => {
    expect(roleWorkLine(input({ items: [{ id: "3", type: "error", message: "Rate limited" }] }))).toMatchObject({ kind: "error", text: "Error: Rate limited" });
    const items = [
      { id: "2", type: "message", role: "user", office_relayed: true, content: [{ type: "input_text", text: "From the PM" }] },
      said("1", "Plan ready."),
    ];
    expect(roleWorkLine(input({ items }))).toMatchObject({ text: "Last said: Plan ready." });
  });

  it("says what will start a role that has no session yet", () => {
    expect(roleWorkLine(input({ sessionId: null }))).toMatchObject({ kind: "not_started", text: "Not started" });
    expect(roleWorkLine(input({ sessionId: null, tasks: [{ title: "Deploy", status: "pending" }] }))).toMatchObject({
      text: "Assigned: Deploy (starts when the PM dispatches it)",
    });
  });

  it("keeps the line to one short line", () => {
    expect(roleWorkLine(input({ items: [said("1", "x".repeat(400))] })).text.length).toBeLessThanOrEqual(160);
  });
});
