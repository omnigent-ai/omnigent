import {
  getNodeDepth,
  hasNodeFlag,
  isDirectoryNode,
  PATH_STORE_NODE_FLAG_ROOT,
} from "./internal-types";
import type { NodeId, PathStoreNode, PathStoreSnapshot } from "./internal-types";
import type {
  PathStoreEvent,
  PathStoreInitialExpansion,
  PathStoreSemanticEvent,
} from "./public-types";

export interface TransactionFrame {
  readonly affectedAncestorIds: Set<NodeId>;
  readonly affectedNodeIds: Set<NodeId>;
  readonly events: PathStoreSemanticEvent[];
}

export interface PathStoreState {
  activeNodeCount: number;
  collapsedDirectoryIds: Set<NodeId>;
  defaultExpansion: PathStoreInitialExpansion;
  directoriesOpenByDefault: boolean;
  hasCollapsedDirectoryOverrides: boolean;
  expandedDirectoryIds: Set<NodeId>;
  listeners: Map<string, Set<(event: PathStoreEvent) => void>>;
  pathCacheByNodeId: Map<NodeId, { path: string; version: number }>;
  pathCacheVersion: number;
  snapshot: PathStoreSnapshot;
  transactionStack: TransactionFrame[];
}

export function createPathStoreState(
  snapshot: PathStoreSnapshot,
  initialExpansion: PathStoreInitialExpansion = "closed",
): PathStoreState {
  const defaultExpansion = resolveInitialExpansion(initialExpansion);
  return {
    activeNodeCount: snapshot.nodes.length - 1,
    collapsedDirectoryIds: new Set<NodeId>(),
    defaultExpansion,
    directoriesOpenByDefault: defaultExpansion === "open",
    hasCollapsedDirectoryOverrides: false,
    expandedDirectoryIds: new Set<NodeId>(),
    listeners: new Map<string, Set<(event: PathStoreEvent) => void>>(),
    pathCacheByNodeId: new Map<NodeId, { path: string; version: number }>([
      [snapshot.rootId, { path: "", version: 0 }],
    ]),
    pathCacheVersion: 0,
    snapshot,
    transactionStack: [],
  };
}

export function createTransactionFrame(): TransactionFrame {
  return {
    affectedAncestorIds: new Set<NodeId>(),
    affectedNodeIds: new Set<NodeId>(),
    events: [],
  };
}

function resolveInitialExpansion(
  initialExpansion: PathStoreInitialExpansion,
): PathStoreInitialExpansion {
  if (typeof initialExpansion !== "number") {
    return initialExpansion;
  }

  if (!Number.isInteger(initialExpansion) || initialExpansion < 0) {
    throw new Error(
      `initialExpansion must be "open", "closed", or a non-negative integer depth. Received: ${String(
        initialExpansion,
      )}`,
    );
  }

  return initialExpansion;
}

function isDirectoryExpandedByDefault(state: PathStoreState, node: PathStoreNode): boolean {
  if (hasNodeFlag(node, PATH_STORE_NODE_FLAG_ROOT)) {
    return true;
  }

  if (state.defaultExpansion === "open") {
    return true;
  }

  if (state.defaultExpansion === "closed") {
    return false;
  }

  return getNodeDepth(node) <= state.defaultExpansion;
}

export function isDirectoryExpanded(
  state: PathStoreState,
  nodeId: NodeId,
  node: PathStoreNode | undefined = state.snapshot.nodes[nodeId],
): boolean {
  if (node == null || !isDirectoryNode(node)) {
    return false;
  }

  if (state.directoriesOpenByDefault && !state.hasCollapsedDirectoryOverrides) {
    return true;
  }

  if (state.collapsedDirectoryIds.has(nodeId)) {
    return false;
  }

  if (state.expandedDirectoryIds.has(nodeId)) {
    return true;
  }

  return isDirectoryExpandedByDefault(state, node);
}

export function setDirectoryExpanded(
  state: PathStoreState,
  nodeId: NodeId,
  expanded: boolean,
  node: PathStoreNode | undefined = state.snapshot.nodes[nodeId],
): void {
  if (node == null || !isDirectoryNode(node)) {
    return;
  }

  const expandedByDefault = isDirectoryExpandedByDefault(state, node);
  if (expanded) {
    if (expandedByDefault) {
      state.collapsedDirectoryIds.delete(nodeId);
      state.hasCollapsedDirectoryOverrides = state.collapsedDirectoryIds.size > 0;
      return;
    }

    state.expandedDirectoryIds.add(nodeId);
    return;
  }

  if (expandedByDefault) {
    state.collapsedDirectoryIds.add(nodeId);
    state.hasCollapsedDirectoryOverrides = true;
    return;
  }

  state.expandedDirectoryIds.delete(nodeId);
}
