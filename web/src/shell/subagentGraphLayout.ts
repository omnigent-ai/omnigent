import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { MAX_TREE_DEPTH } from "@/hooks/useChildSessions";
import { WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";
import { childStatus, type AgentActivity } from "./subagentStatus";

export type { AgentActivity };

interface AgentIdentity {
  nodeKind: "root" | "child";
  wrapper: string | null;
  tool: string | null;
  harness: string | null;
  agentName: string | null;
}

export interface AgentNodeData extends AgentIdentity {
  label: string;
  activity: AgentActivity;
  statusLabel: string;
  sessionId: string;
  isActive: boolean;
  preview: string | null;
  [key: string]: unknown;
}

export interface TreeNode extends AgentIdentity {
  label: string;
  activity: AgentActivity;
  statusLabel: string;
  preview: string | null;
  id: string;
  children: TreeNode[];
}

export interface RootAgentIdentity {
  wrapper: string | null;
  harness: string | null;
  agentName: string | null;
}

interface ChildAgentIdentity extends Pick<AgentIdentity, "wrapper" | "tool"> {
  nodeKind: "child";
  harness: null;
  agentName: null;
}

type NodeIdentity = ({ nodeKind: "root" } & RootAgentIdentity) | ChildAgentIdentity;

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
    const width = Math.max(NODE_WIDTH, total);
    widths.set(n.id, width);
    return width;
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
        nodeKind: node.nodeKind,
        wrapper: node.wrapper,
        tool: node.tool,
        harness: node.harness,
        agentName: node.agentName,
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
      const isWorking = child.activity === "working";

      edges.push({
        id: `${node.id}->${child.id}`,
        source: node.id,
        target: child.id,
        ...edgeDefaults,
        animated: isWorking,
        style: {
          ...edgeDefaults.style,
          opacity: isWorking ? 0.6 : 0.3,
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
  identity: NodeIdentity,
  visited = new Set<string>(),
): TreeNode {
  visited.add(rootId);
  const children = childrenMap.get(rootId) ?? [];
  return {
    id: rootId,
    label: rootLabel,
    activity: rootActivity,
    statusLabel: rootStatusLabel,
    preview: rootPreview,
    nodeKind: identity.nodeKind,
    wrapper: identity.wrapper,
    tool: identity.nodeKind === "child" ? identity.tool : null,
    harness: identity.harness,
    agentName: identity.agentName,
    children:
      depth >= MAX_TREE_DEPTH
        ? []
        : children
            .filter((child) => !visited.has(child.id))
            .map((child) => {
              const status = childActivity(child);
              const label =
                child.task_summary ?? child.session_name ?? child.title ?? child.tool ?? child.id;
              return buildTree(
                child.id,
                label,
                status.activity,
                status.label,
                child.last_message_preview,
                childrenMap,
                depth + 1,
                {
                  nodeKind: "child",
                  wrapper: child.labels?.[WRAPPER_LABEL_KEY] ?? null,
                  tool: child.tool,
                  harness: null,
                  agentName: null,
                },
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
  rootIdentity: RootAgentIdentity,
): { nodes: LayoutNode[]; edges: LayoutEdge[] } {
  const tree = buildTree(
    rootId,
    rootLabel,
    rootActivity,
    rootStatusLabel,
    rootPreview,
    childrenMap,
    0,
    { nodeKind: "root", ...rootIdentity },
  );
  return layoutTree(tree, activeId);
}
