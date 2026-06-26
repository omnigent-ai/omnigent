/**
 * Top-down flow *tree* model — the editing representation behind the guided
 * stepper builder.
 *
 * Unlike the free-form {@link FlowGraph} (a flat node/edge soup), a tree encodes
 * the "add the next step" interaction directly: every step links to the step
 * that follows it. A decision splits into two labelled branches (`yes` / `no`),
 * each of which is its own downward chain; every other step has a single
 * `next`. An `end` step terminates its chain.
 *
 * The tree is the canonical thing the builder edits and persists. For text
 * generation, runs, and Mermaid we convert it to a {@link FlowGraph} via
 * {@link treeToGraph} — so the generator and everything downstream are
 * unchanged. All operations here are pure (they return a new tree); the React
 * layer holds the current tree in state and swaps it on each edit.
 */

import type { FlowEdge, FlowGraph, FlowNode, FlowNodeType } from "./flowToText";

export interface FlowStep {
  id: string;
  type: FlowNodeType;
  label: string;
  /** Successor for non-decision steps (null = open slot, shows a "+"). */
  next: FlowStep | null;
  /** Decision true-branch head + its label. */
  yes: FlowStep | null;
  yesLabel: string;
  /** Decision false-branch head + its label. */
  no: FlowStep | null;
  noLabel: string;
}

/** Which child slot of a step an add/attach targets. */
export type Slot = "next" | "yes" | "no";

/** Types offerable when adding a step (Start only ever exists as the root). */
export const ADDABLE_TYPES: FlowNodeType[] = ["process", "decision", "io", "end"];

export function defaultLabel(type: FlowNodeType): string {
  switch (type) {
    case "start":
      return "Start";
    case "process":
      return "Do something";
    case "decision":
      return "Condition?";
    case "io":
      return "Input / Output";
    case "end":
      return "End";
  }
}

let stepCounter = 1;
const stepId = (type: FlowNodeType) =>
  `${type}_${stepCounter++}_${Math.random().toString(36).slice(2, 7)}`;

export function newStep(type: FlowNodeType, label?: string): FlowStep {
  return {
    id: stepId(type),
    type,
    label: label ?? defaultLabel(type),
    next: null,
    yes: null,
    yesLabel: "Yes",
    no: null,
    noLabel: "No",
  };
}

/** A fresh tree: a single Start step with an open next slot. */
export function newTree(): FlowStep {
  return newStep("start");
}

/** Map every step in the tree, returning a new tree (pure). */
function mapTree(step: FlowStep, fn: (s: FlowStep) => FlowStep): FlowStep {
  const mapped = fn(step);
  return {
    ...mapped,
    next: mapped.next ? mapTree(mapped.next, fn) : null,
    yes: mapped.yes ? mapTree(mapped.yes, fn) : null,
    no: mapped.no ? mapTree(mapped.no, fn) : null,
  };
}

/** Attach a new step of `type` to `parentId`'s `slot`. No-op if slot is taken. */
export function attach(root: FlowStep, parentId: string, slot: Slot, type: FlowNodeType): FlowStep {
  return mapTree(root, (s) => {
    if (s.id !== parentId || s[slot]) return s;
    return { ...s, [slot]: newStep(type) };
  });
}

/** Rename a step's label. */
export function setLabel(root: FlowStep, id: string, label: string): FlowStep {
  return mapTree(root, (s) => (s.id === id ? { ...s, label } : s));
}

/** Rename a decision branch label (`yes`/`no`). */
export function setBranchLabel(
  root: FlowStep,
  id: string,
  branch: "yes" | "no",
  label: string,
): FlowStep {
  const key = branch === "yes" ? "yesLabel" : "noLabel";
  return mapTree(root, (s) => (s.id === id ? { ...s, [key]: label } : s));
}

/**
 * Remove a step and everything below it. Returns the new tree, or `null` if the
 * deleted step was the root (caller decides what to seed in its place).
 */
export function deleteStep(root: FlowStep, id: string): FlowStep | null {
  if (root.id === id) return null;
  return mapTree(root, (s) => ({
    ...s,
    next: s.next?.id === id ? null : s.next,
    yes: s.yes?.id === id ? null : s.yes,
    no: s.no?.id === id ? null : s.no,
  }));
}

/** Total number of steps in the tree. */
export function countSteps(step: FlowStep | null): number {
  if (!step) return 0;
  return 1 + countSteps(step.next) + countSteps(step.yes) + countSteps(step.no);
}

/**
 * Convert a tree to the flat {@link FlowGraph} the generator consumes. Node
 * geometry is irrelevant here (the tree carries no loops, and the generator
 * only uses coordinates for loop-membership), so positions are left at 0.
 */
export function treeToGraph(root: FlowStep): FlowGraph {
  const nodes: FlowNode[] = [];
  const edges: FlowEdge[] = [];
  const edge = (from: string, to: string, label?: string) =>
    edges.push({ id: `e_${from}_${to}`, from, to, label });

  function walk(step: FlowStep) {
    nodes.push({ id: step.id, type: step.type, label: step.label, x: 0, y: 0 });
    if (step.type === "decision") {
      if (step.yes) {
        edge(step.id, step.yes.id, step.yesLabel || "Yes");
        walk(step.yes);
      }
      if (step.no) {
        edge(step.id, step.no.id, step.noLabel || "No");
        walk(step.no);
      }
    } else if (step.next) {
      edge(step.id, step.next.id);
      walk(step.next);
    }
  }
  walk(root);
  return { nodes, edges, loops: [] };
}
