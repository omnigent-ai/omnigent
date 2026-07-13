import type { WorkflowSummary } from "@/hooks/useWorkflows";

export const WORKFLOW_NODE_WIDTH = 190;
const NODE_HEIGHT = 86;
const COLUMN_GAP = 72;
const ROW_GAP = 28;

export interface WorkflowGraphNodeData {
  node: WorkflowSummary["nodes"][number];
  [key: string]: unknown;
}

export interface WorkflowLayoutNode {
  id: string;
  type: "workflow";
  position: { x: number; y: number };
  data: WorkflowGraphNodeData;
}

export interface WorkflowLayoutEdge {
  id: string;
  source: string;
  target: string;
  animated: boolean;
  style: { stroke: string; strokeWidth: number; opacity: number };
}

export function buildWorkflowGraphLayout(workflow: WorkflowSummary): {
  nodes: WorkflowLayoutNode[];
  edges: WorkflowLayoutEdge[];
} {
  const byId = new Map(workflow.nodes.map((node) => [node.id, node]));
  const layers = new Map<string, number>();

  const layerFor = (nodeId: string, visiting = new Set<string>()): number => {
    const cached = layers.get(nodeId);
    if (cached !== undefined) return cached;
    if (visiting.has(nodeId)) return 0;
    visiting.add(nodeId);
    const node = byId.get(nodeId);
    const layer = node?.deps.length
      ? Math.max(...node.deps.map((dep) => layerFor(dep, visiting))) + 1
      : 0;
    visiting.delete(nodeId);
    layers.set(nodeId, layer);
    return layer;
  };

  for (const node of workflow.nodes) layerFor(node.id);
  const columns = new Map<number, WorkflowSummary["nodes"]>();
  for (const node of workflow.nodes) {
    const layer = layers.get(node.id) ?? 0;
    columns.set(layer, [...(columns.get(layer) ?? []), node]);
  }
  const canvasHeight = Math.max(
    ...[...columns.values()].map(
      (column) => column.length * NODE_HEIGHT + Math.max(0, column.length - 1) * ROW_GAP,
    ),
  );

  const nodes: WorkflowLayoutNode[] = [];
  for (const [layer, column] of [...columns.entries()].sort(([a], [b]) => a - b)) {
    const height = column.length * NODE_HEIGHT + Math.max(0, column.length - 1) * ROW_GAP;
    column.forEach((node, index) => {
      nodes.push({
        id: node.id,
        type: "workflow",
        position: {
          x: layer * (WORKFLOW_NODE_WIDTH + COLUMN_GAP),
          y: (canvasHeight - height) / 2 + index * (NODE_HEIGHT + ROW_GAP),
        },
        data: { node },
      });
    });
  }

  const edges = workflow.nodes.flatMap((node) =>
    node.deps.map((dep) => ({
      id: `${dep}->${node.id}`,
      source: dep,
      target: node.id,
      animated: node.state === "running",
      style: {
        stroke: "var(--muted-foreground)",
        strokeWidth: 1.5,
        opacity: node.state === "running" ? 0.65 : 0.3,
      },
    })),
  );
  return { nodes, edges };
}
