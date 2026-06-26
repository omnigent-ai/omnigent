import { describe, expect, it } from "vitest";
import {
  attach,
  countSteps,
  deleteStep,
  newTree,
  setBranchLabel,
  setLabel,
  treeToGraph,
} from "./flowTree";

describe("flowTree", () => {
  it("starts as a single Start step", () => {
    const t = newTree();
    expect(t.type).toBe("start");
    expect(countSteps(t)).toBe(1);
    expect(t.next).toBeNull();
  });

  it("attaches a next step to an open slot", () => {
    let t = newTree();
    t = attach(t, t.id, "next", "process");
    expect(countSteps(t)).toBe(2);
    expect(t.next?.type).toBe("process");
  });

  it("does not overwrite a filled slot", () => {
    let t = newTree();
    t = attach(t, t.id, "next", "process");
    const firstNextId = t.next!.id;
    t = attach(t, t.id, "next", "end"); // slot taken → no-op
    expect(t.next?.id).toBe(firstNextId);
    expect(countSteps(t)).toBe(2);
  });

  it("branches a decision into yes/no lanes", () => {
    let t = newTree();
    t = attach(t, t.id, "next", "decision");
    const dId = t.next!.id;
    t = attach(t, dId, "yes", "process");
    t = attach(t, dId, "no", "end");
    expect(t.next?.yes?.type).toBe("process");
    expect(t.next?.no?.type).toBe("end");
    expect(countSteps(t)).toBe(4);
  });

  it("renames steps and branch labels", () => {
    let t = newTree();
    t = attach(t, t.id, "next", "decision");
    const dId = t.next!.id;
    t = setLabel(t, dId, "More orders?");
    t = setBranchLabel(t, dId, "yes", "Keep going");
    expect(t.next?.label).toBe("More orders?");
    expect(t.next?.yesLabel).toBe("Keep going");
  });

  it("deletes a step and its subtree, but never the root", () => {
    let t = newTree();
    t = attach(t, t.id, "next", "process");
    const pId = t.next!.id;
    t = attach(t, pId, "next", "end");
    expect(countSteps(t)).toBe(3);
    t = deleteStep(t, pId) ?? t;
    expect(countSteps(t)).toBe(1); // process + its end removed
    expect(deleteStep(t, t.id)).toBeNull(); // deleting root returns null
  });

  it("converts a decision tree to a graph with labelled branch edges", () => {
    let t = newTree();
    t = setLabel(t, t.id, "Begin");
    t = attach(t, t.id, "next", "decision");
    const dId = t.next!.id;
    t = setLabel(t, dId, "OK?");
    t = attach(t, dId, "yes", "process");
    t = attach(t, dId, "no", "end");

    const g = treeToGraph(t);
    expect(g.nodes).toHaveLength(4);
    // Start → decision (unlabelled), decision → yes (Yes), decision → no (No)
    const labels = g.edges.map((e) => e.label ?? "").sort();
    expect(labels).toContain("Yes");
    expect(labels).toContain("No");
    // The generator accepts this graph end-to-end.
    expect(g.loops).toEqual([]);
  });
});
