import { describe, expect, it } from "vitest";

import {
  layoutWorkflowSteps,
  STEP_NAME_ONLY_HEIGHT,
  STEP_NODE_HEIGHT,
  STEP_NODE_WIDTH,
  stepRows,
  type WorkflowStep,
} from "./workflowGraphLayout";

function step(id: string, ...dependsOn: string[]): WorkflowStep {
  return { id, name: id, goal: null, depends_on: dependsOn };
}

// explore → reproduce → (fix, history) → pr
const STEPS = [
  step("explore"),
  step("reproduce", "explore"),
  step("fix", "reproduce"),
  step("history", "reproduce"),
  step("pr", "fix", "history"),
];

describe("stepRows", () => {
  it("puts a step one row below the lowest step it waits for", () => {
    expect(Object.fromEntries(stepRows(STEPS))).toEqual({
      explore: 0,
      reproduce: 1,
      fix: 2,
      history: 2,
      pr: 3,
    });
    // A step that waits on two rows sits below the lower one.
    expect(stepRows([step("a"), step("b", "a"), step("c", "a", "b")]).get("c")).toBe(2);
  });

  it("ignores a missing dependency and survives a cycle", () => {
    expect(stepRows([step("a", "gone")]).get("a")).toBe(0);
    const rows = stepRows([step("a", "b"), step("b", "a")]);
    expect([...rows.keys()].sort()).toEqual(["a", "b"]);
  });
});

describe("layoutWorkflowSteps", () => {
  it("lays steps out top to bottom, centering each row on the widest", () => {
    const { nodes, height } = layoutWorkflowSteps(STEPS);
    const at = Object.fromEntries(nodes.map((node) => [node.id, node.position]));

    // Four rows of names, 28px apart.
    expect(height).toBe(4 * STEP_NAME_ONLY_HEIGHT + 3 * 28);
    expect(at.fix!.y).toBe(at.history!.y);
    expect(at.pr!.y).toBe(3 * (STEP_NAME_ONLY_HEIGHT + 28));
    // The two-step row keeps the workflow's order; one-step rows sit between them.
    expect(at.fix!.x).toBeLessThan(at.history!.x);
    expect(at.explore!.x).toBe((at.fix!.x + at.history!.x) / 2);
    expect(at.history!.x - at.fix!.x).toBe(STEP_NODE_WIDTH + 32);
    expect(nodes.find((node) => node.id === "fix")?.data).toEqual({
      name: "fix",
      goal: null,
      number: 3,
    });
  });

  it("makes room for a goal only in the rows that have one", () => {
    const withGoal = { ...step("fix", "explore"), goal: "The suite passes" };
    const { nodes, height } = layoutWorkflowSteps([step("explore"), withGoal, step("pr", "fix")]);
    const at = Object.fromEntries(nodes.map((node) => [node.id, node.position]));

    expect(at.fix!.y).toBe(STEP_NAME_ONLY_HEIGHT + 28);
    expect(at.pr!.y).toBe(at.fix!.y + STEP_NODE_HEIGHT + 28);
    expect(height).toBe(at.pr!.y + STEP_NAME_ONLY_HEIGHT);
    expect(layoutWorkflowSteps([]).height).toBe(0);
  });

  it("gives a step that can't start its failed steps' names and room for them", () => {
    const failedNames = new Map([["pr", ["fix"]]]);
    const { nodes, height } = layoutWorkflowSteps(
      [step("fix"), step("pr", "fix")],
      undefined,
      failedNames,
    );

    expect(nodes.find((node) => node.id === "pr")?.data.failedNames).toEqual(["fix"]);
    expect(nodes.find((node) => node.id === "fix")?.data.failedNames).toBeUndefined();
    // pr has no goal, but its row is as tall as one with a goal.
    expect(height).toBe(STEP_NAME_ONLY_HEIGHT + 28 + STEP_NODE_HEIGHT);
  });

  it("gives each node its run state, and moves the edges into a running step", () => {
    const states = new Map([
      ["explore", "done" as const],
      ["reproduce", "running" as const],
    ]);
    const { nodes, edges } = layoutWorkflowSteps(STEPS, states);

    expect(nodes.find((node) => node.id === "reproduce")?.data.state).toBe("running");
    expect(nodes.find((node) => node.id === "fix")?.data.state).toBeUndefined();
    expect(edges.filter((edge) => edge.animated).map((edge) => edge.id)).toEqual([
      "explore->reproduce",
    ]);
  });

  it("draws an edge from each step to each step that waits for it", () => {
    const { edges } = layoutWorkflowSteps([...STEPS, step("lost", "gone")]);

    expect(edges.map((edge) => edge.id)).toEqual([
      "explore->reproduce",
      "reproduce->fix",
      "reproduce->history",
      "fix->pr",
      "history->pr",
    ]);
  });
});
