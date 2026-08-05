import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { MAX_TREE_DEPTH } from "@/hooks/useChildSessions";
import { WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";
import { childStatus, type AgentActivity } from "./subagentStatus";

export type { AgentActivity };

// Raw identity fields the node component resolves into a harness/role glyph.
// Kept as plain strings so this module stays free of React and icon imports.
export interface AgentIconFields {
  /** ``omnigent.wrapper`` label, when the session carries one. */
  wrapper: string | null;
  /** Sub-agent type, e.g. ``"Explore"``; ``null`` on the root node. */
  tool: string | null;
  /** Agent name — resolves the root's nessie glyph. ``null`` on children. */
  agentName: string | null;
  /** Resolved harness, for SDK sessions carrying no wrapper label. */
  harness: string | null;
}

export interface AgentNodeData extends AgentIconFields {
  label: string;
  activity: AgentActivity;
  statusLabel: string;
  sessionId: string;
  isActive: boolean;
  preview: string | null;
  /** True for the root node, which resolves its glyph as a whole session
   *  (brand → nessie → bot) rather than as a typed sub-agent. */
  isRoot: boolean;
  [key: string]: unknown;
}

export interface TreeNode extends AgentIconFields {
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
  const status = childStatus(child);
  return { activity: status.activity, label: status.label };
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

  function place(node: TreeNode, x: number, y: number, isRoot: boolean) {
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
        wrapper: node.wrapper,
        tool: node.tool,
        agentName: node.agentName,
        harness: node.harness,
        isRoot,
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

      place(child, childCenterX, childY, false);
      childX += childWidth + HORIZONTAL_GAP;
    }
  }

  place(root, 0, 0, true);
  return { nodes, edges };
}

/** The subtree root's own fields. Grouped rather than passed positionally —
 *  the identity fields the node icons need would otherwise push this to ten
 *  positional arguments. */
export interface TreeRoot extends Partial<AgentIconFields> {
  id: string;
  label: string;
  activity: AgentActivity;
  statusLabel: string;
  preview: string | null;
}

export function buildTree(
  root: TreeRoot,
  childrenMap: Map<string, ChildSessionInfo[]>,
  depth: number,
  visited = new Set<string>(),
): TreeNode {
  visited.add(root.id);
  const children = childrenMap.get(root.id) ?? [];
  return {
    id: root.id,
    label: root.label,
    activity: root.activity,
    statusLabel: root.statusLabel,
    preview: root.preview,
    wrapper: root.wrapper ?? null,
    tool: root.tool ?? null,
    agentName: root.agentName ?? null,
    harness: root.harness ?? null,
    children:
      depth >= MAX_TREE_DEPTH
        ? []
        : children
            .filter((child) => !visited.has(child.id))
            .map((child) => {
              const status = childActivity(child);
              const label = child.session_name ?? child.title ?? child.tool ?? child.id;
              return buildTree(
                {
                  id: child.id,
                  label,
                  activity: status.activity,
                  statusLabel: status.label,
                  preview: child.last_message_preview,
                  wrapper: child.labels?.[WRAPPER_LABEL_KEY] ?? null,
                  tool: child.tool,
                },
                childrenMap,
                depth + 1,
                visited,
              );
            }),
  };
}

export function buildGraphLayout(
  root: TreeRoot,
  childrenMap: Map<string, ChildSessionInfo[]>,
  activeId: string,
): { nodes: LayoutNode[]; edges: LayoutEdge[] } {
  return layoutTree(buildTree(root, childrenMap, 0), activeId);
}
