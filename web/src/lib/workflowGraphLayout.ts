import { type Edge, MarkerType, type Node } from "@xyflow/react";

/** One step of a saved workflow, as `GET /v1/workflows/{id}` returns it. */
export interface WorkflowStep {
  /** Short and stable, e.g. `"explore"`. */
  id: string;
  /** A few words, e.g. `"Explore the failing tests"`. */
  name: string;
  /** What done means for the step, or `null`. */
  goal: string | null;
  /** The IDs of the steps it waits for. */
  depends_on: string[];
}

/** What a step's node shows. */
export interface StepNodeData extends Record<string, unknown> {
  name: string;
  goal: string | null;
}

export const STEP_NODE_WIDTH = 220;
// Room for the name and two lines of the goal.
export const STEP_NODE_HEIGHT = 72;
const COLUMN_GAP = 32;
const ROW_GAP = 40;

const EDGE_STYLE = { stroke: "var(--muted-foreground)", strokeWidth: 1.5, opacity: 0.4 };

/**
 * Each step's row: one below the lowest step it waits for, so steps that can
 * run at the same time share a row. A dependency the workflow doesn't have,
 * or a cycle, is ignored; saved workflows have neither.
 */
export function stepRows(steps: readonly WorkflowStep[]): Map<string, number> {
  const byId = new Map(steps.map((step) => [step.id, step]));
  const rows = new Map<string, number>();
  const visiting = new Set<string>();
  const rowOf = (step: WorkflowStep): number => {
    const known = rows.get(step.id);
    if (known !== undefined) return known;
    if (visiting.has(step.id)) return 0;
    visiting.add(step.id);
    let row = 0;
    for (const dependency of step.depends_on) {
      const before = byId.get(dependency);
      if (before) row = Math.max(row, rowOf(before) + 1);
    }
    visiting.delete(step.id);
    rows.set(step.id, row);
    return row;
  };
  for (const step of steps) rowOf(step);
  return rows;
}

/**
 * Lay a workflow's steps out top to bottom by dependency, for React Flow.
 * Within a row, steps keep the workflow's order, and each row is centered.
 *
 * @returns The nodes, an edge from each step to each step that waits for it,
 *   and how many rows the steps take.
 */
export function layoutWorkflowSteps(steps: readonly WorkflowStep[]): {
  nodes: Node<StepNodeData>[];
  edges: Edge[];
  rowCount: number;
} {
  const rows = stepRows(steps);
  const byRow = new Map<number, WorkflowStep[]>();
  for (const step of steps) {
    const row = rows.get(step.id) ?? 0;
    byRow.set(row, [...(byRow.get(row) ?? []), step]);
  }
  const widest = Math.max(0, ...[...byRow.values()].map((row) => row.length));
  const nodes: Node<StepNodeData>[] = [];
  for (const [row, members] of byRow) {
    const offset = ((widest - members.length) * (STEP_NODE_WIDTH + COLUMN_GAP)) / 2;
    members.forEach((step, index) => {
      nodes.push({
        id: step.id,
        type: "step",
        position: {
          x: offset + index * (STEP_NODE_WIDTH + COLUMN_GAP),
          y: row * (STEP_NODE_HEIGHT + ROW_GAP),
        },
        data: { name: step.name, goal: step.goal },
      });
    });
  }
  const known = new Set(steps.map((step) => step.id));
  const edges: Edge[] = steps.flatMap((step) =>
    step.depends_on
      .filter((dependency) => known.has(dependency))
      .map((dependency) => ({
        id: `${dependency}->${step.id}`,
        source: dependency,
        target: step.id,
        style: EDGE_STYLE,
        markerEnd: { type: MarkerType.ArrowClosed, color: "var(--muted-foreground)" },
      })),
  );
  return { nodes, edges, rowCount: byRow.size };
}
