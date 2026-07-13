import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { MAX_TREE_DEPTH } from "@/hooks/useChildSessions";

export type AgentActivity =
  | "launching"
  | "working"
  | "awaiting"
  | "done"
  | "failed"
  | "idle"
  | "other";

export interface AgentNodeData {
  label: string;
  activity: AgentActivity;
  statusLabel: string;
  sessionId: string;
  isActive: boolean;
  preview: string | null;
  [key: string]: unknown;
}

export interface TreeNode {
  id: string;
  label: string;
  activity: AgentActivity;
  statusLabel: string;
  preview: string | null;
  children: TreeNode[];
}

interface LayoutNode {
  id: string;
  type: string;
  position: { x: number; y: number };
  data: AgentNodeData;
}

interface LayoutEdge {
  id: string;
  source: string;
  target: string;
  type: string;
  animated: boolean;
  style: { stroke: string; strokeWidth: number; opacity: number };
}

export const NODE_WIDTH = 180;
const NODE_HEIGHT = 60;
const HORIZONTAL_GAP = 40;
const VERTICAL_GAP = 30;

export function childActivity(child: ChildSessionInfo): { activity: AgentActivity; label: string } {
  if (child.pending_elicitations_count > 0)
    return { activity: "awaiting", label: "Needs response" };
  if (child.current_task_status === "launching")
    return { activity: "launching", label: "Launching" };
  if (child.busy) return { activity: "working", label: "Working" };
  if (child.last_task_error) return { activity: "failed", label: "Failed" };
  if (child.current_task_status === "failed") return { activity: "failed", label: "Failed" };
  if (child.current_task_status === "completed") return { activity: "done", label: "Done" };
  if (child.current_task_status) return { activity: "other", label: child.current_task_status };
  return { activity: "idle", label: "Idle" };
}

const edgeDefaults = {
  type: "default",
  animated: false,
  style: { stroke: "var(--muted-foreground)", strokeWidth: 1.5, opacity: 0.3 },
};

export function computeSubtreeWidths(node: TreeNode): Map<string, number> {
  const widths = new Map<string, number>();
  function walk(n: TreeNode): number {
    if (n.children.length === 0) {
      widths.set(n.id, NODE_WIDTH);
      return NODE_WIDTH;
    }
    const total =
      n.children.reduce((sum, c) => sum + walk(c), 0) + (n.children.length - 1) * HORIZONTAL_GAP;
    widths.set(n.id, Math.max(NODE_WIDTH, total));
    return Math.max(NODE_WIDTH, total);
  }
  walk(node);
  return widths;
}

export function layoutTree(
  root: TreeNode,
  activeId: string,
): { nodes: LayoutNode[]; edges: LayoutEdge[] } {
  const subtreeWidths = computeSubtreeWidths(root);
  const nodes: LayoutNode[] = [];
  const edges: LayoutEdge[] = [];

  function place(node: TreeNode, x: number, y: number) {
    nodes.push({
      id: node.id,
      type: "agent",
      position: { x: x - NODE_WIDTH / 2, y },
      data: {
        label: node.label,
        activity: node.activity,
        statusLabel: node.statusLabel,
        sessionId: node.id,
        isActive: node.id === activeId,
        preview: node.preview,
      },
    });

    if (node.children.length === 0) return;

    const childY = y + NODE_HEIGHT + VERTICAL_GAP;
    const totalChildrenWidth =
      node.children.reduce((sum, c) => sum + (subtreeWidths.get(c.id) ?? NODE_WIDTH), 0) +
      (node.children.length - 1) * HORIZONTAL_GAP;

    let childX = x - totalChildrenWidth / 2;

    for (const child of node.children) {
      const childWidth = subtreeWidths.get(child.id) ?? NODE_WIDTH;
      const childCenterX = childX + childWidth / 2;

      edges.push({
        id: `${node.id}->${child.id}`,
        source: node.id,
        target: child.id,
        ...edgeDefaults,
        animated: child.activity === "working",
        style: {
          ...edgeDefaults.style,
          opacity: child.activity === "working" ? 0.6 : 0.3,
        },
      });

      place(child, childCenterX, childY);
      childX += childWidth + HORIZONTAL_GAP;
    }
  }

  place(root, 0, 0);
  return { nodes, edges };
}

export function buildTree(
  rootId: string,
  rootLabel: string,
  rootActivity: AgentActivity,
  rootStatusLabel: string,
  rootPreview: string | null,
  childrenMap: Map<string, ChildSessionInfo[]>,
  depth: number,
  visited: Set<string> = new Set(),
): TreeNode {
  visited.add(rootId);
  const children = childrenMap.get(rootId) ?? [];
  return {
    id: rootId,
    label: rootLabel,
    activity: rootActivity,
    statusLabel: rootStatusLabel,
    preview: rootPreview,
    children:
      depth >= MAX_TREE_DEPTH
        ? []
        : children
            .filter((child) => !visited.has(child.id))
            .map((child) => {
              const status = childActivity(child);
              const label = child.session_name ?? child.title ?? child.tool ?? child.id;
              return buildTree(
                child.id,
                label,
                status.activity,
                status.label,
                child.last_message_preview,
                childrenMap,
                depth + 1,
                visited,
              );
            }),
  };
}

export function buildGraphLayout(
  rootId: string,
  rootLabel: string,
  rootActivity: AgentActivity,
  rootStatusLabel: string,
  rootPreview: string | null,
  childrenMap: Map<string, ChildSessionInfo[]>,
  activeId: string,
): { nodes: LayoutNode[]; edges: LayoutEdge[] } {
  const tree = buildTree(
    rootId,
    rootLabel,
    rootActivity,
    rootStatusLabel,
    rootPreview,
    childrenMap,
    0,
  );
  return layoutTree(tree, activeId);
}
