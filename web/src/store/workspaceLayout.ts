import { create } from "zustand";

export const WORKSPACE_LAYOUT_STORAGE_KEY = "omnigent:workspace-layout";
const WORKSPACE_LAYOUT_VERSION = 1;
const MIN_PANE_SIZE = 20;

type WorkspaceSplitDirection = "horizontal" | "vertical";
export type WorkspaceDropEdge = "left" | "right" | "top" | "bottom";

export interface WorkspaceLeaf {
  kind: "leaf";
  id: string;
  sessionId: string | null;
}

export interface WorkspaceSplit {
  kind: "split";
  id: string;
  direction: WorkspaceSplitDirection;
  children: [WorkspaceNode, WorkspaceNode];
  sizes: [number, number];
}

export type WorkspaceNode = WorkspaceLeaf | WorkspaceSplit;

export interface WorkspaceLayout {
  root: WorkspaceNode;
  focusedPaneId: string;
}

interface PersistedWorkspaceLayout extends WorkspaceLayout {
  version: number;
}

// Ids derive from the current tree rather than a process-local counter, so a
// restored layout can never collide with ids generated after a reload.
function maxIdSuffix(root: WorkspaceNode, prefix: "pane" | "split"): number {
  const own = root.id.startsWith(`${prefix}-`) ? Number(root.id.slice(prefix.length + 1)) : 0;
  const childMax =
    root.kind === "split"
      ? Math.max(maxIdSuffix(root.children[0], prefix), maxIdSuffix(root.children[1], prefix))
      : 0;
  return Math.max(Number.isFinite(own) ? own : 0, childMax);
}

function nextId(prefix: "pane" | "split", root: WorkspaceNode | null): string {
  return `${prefix}-${root ? maxIdSuffix(root, prefix) + 1 : 1}`;
}

function makeLeaf(root: WorkspaceNode | null, sessionId: string | null): WorkspaceLeaf {
  return { kind: "leaf", id: nextId("pane", root), sessionId };
}

export function createWorkspaceLayout(sessionId: string | null = null): WorkspaceLayout {
  const root = makeLeaf(null, sessionId);
  return { root, focusedPaneId: root.id };
}

export function findWorkspaceLeaf(root: WorkspaceNode, paneId: string): WorkspaceLeaf | null {
  if (root.kind === "leaf") return root.id === paneId ? root : null;
  return findWorkspaceLeaf(root.children[0], paneId) ?? findWorkspaceLeaf(root.children[1], paneId);
}

export function findWorkspaceLeafBySession(
  root: WorkspaceNode,
  sessionId: string,
): WorkspaceLeaf | null {
  if (root.kind === "leaf") return root.sessionId === sessionId ? root : null;
  return (
    findWorkspaceLeafBySession(root.children[0], sessionId) ??
    findWorkspaceLeafBySession(root.children[1], sessionId)
  );
}

function replaceNode(
  root: WorkspaceNode,
  nodeId: string,
  replacement: WorkspaceNode,
): WorkspaceNode {
  if (root.id === nodeId) return replacement;
  if (root.kind === "leaf") return root;
  return {
    ...root,
    children: [
      replaceNode(root.children[0], nodeId, replacement),
      replaceNode(root.children[1], nodeId, replacement),
    ],
  };
}

function replaceLeafSession(root: WorkspaceNode, paneId: string, sessionId: string): WorkspaceNode {
  if (root.kind === "leaf") {
    return root.id === paneId ? { ...root, sessionId } : root;
  }
  return {
    ...root,
    children: [
      replaceLeafSession(root.children[0], paneId, sessionId),
      replaceLeafSession(root.children[1], paneId, sessionId),
    ],
  };
}

export function selectWorkspaceSession(
  layout: WorkspaceLayout,
  sessionId: string,
): WorkspaceLayout {
  const existing = findWorkspaceLeafBySession(layout.root, sessionId);
  if (existing) return { ...layout, focusedPaneId: existing.id };

  const focused = findWorkspaceLeaf(layout.root, layout.focusedPaneId);
  const target = focused ?? firstWorkspaceLeaf(layout.root);
  return {
    root: replaceLeafSession(layout.root, target.id, sessionId),
    focusedPaneId: target.id,
  };
}

export function setWorkspaceLeafSession(
  layout: WorkspaceLayout,
  paneId: string,
  sessionId: string,
): WorkspaceLayout {
  // A session shown in two panes at once is never useful; jumping to the
  // pane that already has it matches what selecting it would do.
  const existing = findWorkspaceLeafBySession(layout.root, sessionId);
  if (existing) return { ...layout, focusedPaneId: existing.id };

  const target = findWorkspaceLeaf(layout.root, paneId);
  if (!target) return layout;
  return {
    root: replaceLeafSession(layout.root, paneId, sessionId),
    focusedPaneId: paneId,
  };
}

export function splitWorkspacePane(
  layout: WorkspaceLayout,
  targetPaneId: string,
  sessionId: string,
  edge: WorkspaceDropEdge,
): WorkspaceLayout {
  const target = findWorkspaceLeaf(layout.root, targetPaneId);
  if (!target || findWorkspaceLeafBySession(layout.root, sessionId)) return layout;

  const newLeaf = makeLeaf(layout.root, sessionId);
  const before = edge === "left" || edge === "top";
  const split: WorkspaceSplit = {
    kind: "split",
    id: nextId("split", layout.root),
    direction: edge === "left" || edge === "right" ? "horizontal" : "vertical",
    children: before ? [newLeaf, target] : [target, newLeaf],
    sizes: [50, 50],
  };

  return {
    root: replaceNode(layout.root, targetPaneId, split),
    focusedPaneId: newLeaf.id,
  };
}

function removeLeaf(root: WorkspaceNode, paneId: string): WorkspaceNode | null {
  if (root.kind === "leaf") return root.id === paneId ? null : root;

  const first = removeLeaf(root.children[0], paneId);
  const second = removeLeaf(root.children[1], paneId);
  if (!first) return second;
  if (!second) return first;
  if (first === root.children[0] && second === root.children[1]) return root;
  return { ...root, children: [first, second] };
}

function firstWorkspaceLeaf(root: WorkspaceNode): WorkspaceLeaf {
  return root.kind === "leaf" ? root : firstWorkspaceLeaf(root.children[0]);
}

export function closeWorkspacePane(layout: WorkspaceLayout, paneId: string): WorkspaceLayout {
  if (layout.root.kind === "leaf" || !findWorkspaceLeaf(layout.root, paneId)) return layout;
  const root = removeLeaf(layout.root, paneId);
  if (!root) return layout;

  const focusStillExists = findWorkspaceLeaf(root, layout.focusedPaneId);
  return {
    root,
    focusedPaneId: focusStillExists?.id ?? firstWorkspaceLeaf(root).id,
  };
}

function resizeSplitNode(root: WorkspaceNode, splitId: string, firstSize: number): WorkspaceNode {
  if (root.kind === "leaf") return root;
  if (root.id === splitId) {
    const clamped = Math.max(MIN_PANE_SIZE, Math.min(100 - MIN_PANE_SIZE, firstSize));
    return { ...root, sizes: [clamped, 100 - clamped] };
  }
  return {
    ...root,
    children: [
      resizeSplitNode(root.children[0], splitId, firstSize),
      resizeSplitNode(root.children[1], splitId, firstSize),
    ],
  };
}

export function resizeWorkspaceSplit(
  layout: WorkspaceLayout,
  splitId: string,
  firstSize: number,
): WorkspaceLayout {
  return { ...layout, root: resizeSplitNode(layout.root, splitId, firstSize) };
}

function isLeaf(value: unknown): value is WorkspaceLeaf {
  if (!value || typeof value !== "object") return false;
  const leaf = value as Partial<WorkspaceLeaf>;
  return (
    leaf.kind === "leaf" &&
    typeof leaf.id === "string" &&
    (typeof leaf.sessionId === "string" || leaf.sessionId === null)
  );
}

function isNode(value: unknown): value is WorkspaceNode {
  if (isLeaf(value)) return true;
  if (!value || typeof value !== "object") return false;
  const split = value as Partial<WorkspaceSplit>;
  return (
    split.kind === "split" &&
    typeof split.id === "string" &&
    (split.direction === "horizontal" || split.direction === "vertical") &&
    Array.isArray(split.children) &&
    split.children.length === 2 &&
    isNode(split.children[0]) &&
    isNode(split.children[1]) &&
    Array.isArray(split.sizes) &&
    split.sizes.length === 2 &&
    split.sizes.every((size) => typeof size === "number" && Number.isFinite(size) && size > 0)
  );
}

export function readWorkspaceLayout(): WorkspaceLayout | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(WORKSPACE_LAYOUT_STORAGE_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<PersistedWorkspaceLayout>;
    if (
      value.version !== WORKSPACE_LAYOUT_VERSION ||
      !isNode(value.root) ||
      typeof value.focusedPaneId !== "string" ||
      !findWorkspaceLeaf(value.root, value.focusedPaneId)
    ) {
      return null;
    }
    return { root: value.root, focusedPaneId: value.focusedPaneId };
  } catch {
    return null;
  }
}

export function writeWorkspaceLayout(layout: WorkspaceLayout): void {
  if (typeof window === "undefined") return;
  try {
    const value: PersistedWorkspaceLayout = {
      version: WORKSPACE_LAYOUT_VERSION,
      ...layout,
    };
    window.localStorage.setItem(WORKSPACE_LAYOUT_STORAGE_KEY, JSON.stringify(value));
  } catch {
    // Storage failures must not prevent session navigation.
  }
}

interface WorkspaceLayoutStore extends WorkspaceLayout {
  selectSession: (sessionId: string) => void;
  focusPane: (paneId: string) => void;
  splitPane: (paneId: string, sessionId: string, edge: WorkspaceDropEdge) => void;
  setLeafSession: (paneId: string, sessionId: string) => void;
  closePane: (paneId: string) => void;
  resizeSplit: (splitId: string, firstSize: number) => void;
  reset: (sessionId?: string | null) => void;
}

function persistAndSet(
  set: (layout: Partial<WorkspaceLayoutStore>) => void,
  layout: WorkspaceLayout,
): void {
  writeWorkspaceLayout(layout);
  set(layout);
}

const initialWorkspaceLayout = readWorkspaceLayout() ?? createWorkspaceLayout();

export const useWorkspaceLayoutStore = create<WorkspaceLayoutStore>((set, get) => ({
  ...initialWorkspaceLayout,
  selectSession: (sessionId) => {
    persistAndSet(set, selectWorkspaceSession(get(), sessionId));
  },
  focusPane: (paneId) => {
    if (!findWorkspaceLeaf(get().root, paneId)) return;
    persistAndSet(set, { root: get().root, focusedPaneId: paneId });
  },
  splitPane: (paneId, sessionId, edge) => {
    persistAndSet(set, splitWorkspacePane(get(), paneId, sessionId, edge));
  },
  setLeafSession: (paneId, sessionId) => {
    persistAndSet(set, setWorkspaceLeafSession(get(), paneId, sessionId));
  },
  closePane: (paneId) => {
    persistAndSet(set, closeWorkspacePane(get(), paneId));
  },
  resizeSplit: (splitId, firstSize) => {
    persistAndSet(set, resizeWorkspaceSplit(get(), splitId, firstSize));
  },
  reset: (sessionId = null) => {
    persistAndSet(set, createWorkspaceLayout(sessionId));
  },
}));
