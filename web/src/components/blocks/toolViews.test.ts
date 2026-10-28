import { describe, expect, it } from "vitest";
import { type ToolView, toolViewFor } from "./toolViews";

describe("toolViewFor", () => {
  const plan: ToolView = { summary: () => null };
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

  it("ships with no views registered", () => {
    expect(toolViewFor("manage_todo_list")).toBeUndefined();
  });
});
