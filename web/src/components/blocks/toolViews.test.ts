import { describe, expect, it } from "vitest";
import { type ToolView, toolViewFor } from "./toolViews";

describe("toolViewFor", () => {
  const plan: ToolView = { lineCounts: () => null };
  const views = new Map<string, ToolView>([["manage_todo_list", plan]]);

  it("finds a view by the tool name the harness reports", () => {
    expect(toolViewFor("manage_todo_list", views)).toBe(plan);
  });

  it("resolves Omnigent's MCP-prefixed tools to their bare name", () => {
    expect(toolViewFor("mcp__omnigent__manage_todo_list", views)).toBe(plan);
  });

  it("does not hand another server's tool the bare name's view", () => {
    expect(toolViewFor("mcp__linear__manage_todo_list", views)).toBeUndefined();
    expect(toolViewFor("linear__manage_todo_list", views)).toBeUndefined();
  });

  it("registers one edit view under every harness's edit and write tool name", () => {
    const editView = toolViewFor("Edit");
    expect(editView?.bodyLabel).toBe("Diff");
    for (const name of [
      "MultiEdit",
      "apply_patch",
      "sys_os_edit",
      "mcp__omnigent__sys_os_edit",
      "edit",
      "Write",
      "sys_os_write",
      "mcp__omnigent__sys_os_write",
      "write",
    ]) {
      expect(toolViewFor(name)).toBe(editView);
    }
    expect(toolViewFor("manage_todo_list")).toBeUndefined();
  });
});
