import { describe, expect, it } from "vitest";
import type { WorkflowSummary } from "@/hooks/useWorkflows";
import { buildWorkflowGraphLayout } from "./workflowGraphLayout";

const workflow: WorkflowSummary = {
  workflow_id: "wf",
  name: "Workflow",
  version: 1,
  definition_hash: "hash",
  status: "running",
  blocked_reason: null,
  dispatch_count: 2,
  spent_cost_usd: 0,
  created_at: 1,
  updated_at: 2,
  nodes: [
    {
      id: "a",
      title: "A",
      role: "generic",
      deps: [],
      agent: "codex",
      state: "succeeded",
      attempt_count: 1,
      child_session_id: "conv_a",
      result: {},
      error: null,
    },
    {
      id: "b",
      title: "B",
      role: "generic",
      deps: [],
      agent: "codex",
      state: "running",
      attempt_count: 1,
      child_session_id: "conv_b",
      result: null,
      error: null,
    },
    {
      id: "c",
      title: "C",
      role: "generic",
      deps: ["a", "b"],
      agent: "codex",
      state: "pending",
      attempt_count: 0,
      child_session_id: null,
      result: null,
      error: null,
    },
  ],
};

describe("buildWorkflowGraphLayout", () => {
  it("places dependencies before their consumer and emits both edges", () => {
    const layout = buildWorkflowGraphLayout(workflow);
    const byId = new Map(layout.nodes.map((node) => [node.id, node]));
    expect(byId.get("a")!.position.x).toBeLessThan(byId.get("c")!.position.x);
    expect(byId.get("b")!.position.x).toBeLessThan(byId.get("c")!.position.x);
    expect(layout.edges.map((edge) => edge.id).sort()).toEqual(["a->c", "b->c"]);
  });
});
