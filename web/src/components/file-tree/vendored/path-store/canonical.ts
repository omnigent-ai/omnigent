import {
  applyChildAggregateDelta,
  createDirectoryChildIndex,
  ensureChildIdByNameId,
  rebuildDirectoryChildAggregates,
  rebuildVisibleChildChunks,
  updateChildPositionsFrom,
} from "./child-index";
import { createAddEvent } from "./events";
import {
  collectFlattenedDirectoryChainIds,
  getFlattenedChildDirectoryId,
  getFlattenedTerminalDirectoryId,
} from "./flatten";
import {
  addNodeFlag,
  createNodeDepthAndFlags,
  getNodeDepth,
  getNodeKind,
  hasNodeFlag,
  isDirectoryNode,
  PATH_STORE_NODE_FLAG_EXPLICIT,
  PATH_STORE_NODE_FLAG_ROOT,
  PATH_STORE_NODE_KIND_DIRECTORY,
} from "./internal-types";
import type { DirectoryChildIndex, NodeId, PathStoreNode } from "./internal-types";
import { parseInputPath, parseLookupPath } from "./path";
import type { PathStoreAddEvent, PathStoreCompareEntry } from "./public-types";
import { getSegmentValue, internSegment } from "./segments";
import { compareSegmentSortKeys, getSegmentSortKey } from "./sort";
import { isDirectoryExpanded } from "./state";
import type { PathStoreState } from "./state";

export function listPaths(state: PathStoreState, path?: string): string[] {
  const nodeId = path == null ? state.snapshot.rootId : findNodeId(state, path);
  if (nodeId == null) {
    return [];
  }

  return collectCanonicalEntries(state, nodeId);
}

export function addPath(state: PathStoreState, path: string): PathStoreAddEvent {
  const preparedPath = parseInputPath(path);
  const parentSegments = preparedPath.isDirectory
    ? preparedPath.segments
    : preparedPath.segments.slice(0, -1);
  const previousProjectionSignature = getCollapsedProjectionSignature(
    state,
    findDeepestExistingDirectoryId(state, parentSegments),
  );
  const { createdNodeIds, directoryId } = ensureDirectoryChain(state, parentSegments);

  const affectedNodeIds = new Set<NodeId>(createdNodeIds);
  let addedNodeId = directoryId;

  if (preparedPath.isDirectory) {
    const directoryNode = requireNode(state, directoryId);
    if (hasNodeFlag(directoryNode, PATH_STORE_NODE_FLAG_EXPLICIT)) {
      throw new Error(`Path already exists: "${path}"`);
    }

    addNodeFlag(directoryNode, PATH_STORE_NODE_FLAG_EXPLICIT);
    state.pathCacheByNodeId.set(directoryId, {
      path,
      version: state.pathCacheVersion,
    });
    affectedNodeIds.add(directoryId);
  } else {
    addedNodeId = createFileNode(state, directoryId, preparedPath.basename);
    affectedNodeIds.add(addedNodeId);
  }

  recomputeCountsUpwardFrom(state, directoryId);
  const nextProjectionSignature = getCollapsedProjectionSignature(state, directoryId);
  return createAddEvent({
    affectedAncestorIds: collectAncestorIds(state, addedNodeId),
    affectedNodeIds: [...affectedNodeIds],
    path,
    projectionChanged: didProjectionChange(previousProjectionSignature, nextProjectionSignature),
  });
}

function getCachedNodePath(state: PathStoreState, nodeId: NodeId): string | null {
  const cachedEntry = state.pathCacheByNodeId.get(nodeId);
  return cachedEntry != null && cachedEntry.version === state.pathCacheVersion
    ? cachedEntry.path
    : null;
}

function setCachedNodePath(state: PathStoreState, nodeId: NodeId, path: string): string {
  state.pathCacheByNodeId.set(nodeId, {
    path,
    version: state.pathCacheVersion,
  });
  return path;
}

// Materializes canonical paths only for nodes the caller actually touches, so
// folder moves stay local instead of rewriting descendant strings eagerly.
export function materializeNodePath(state: PathStoreState, nodeId: NodeId): string {
  const node = requireNode(state, nodeId);
  const cachedPath = getCachedNodePath(state, nodeId);
  if (cachedPath != null) {
    return cachedPath;
  }

  if (hasNodeFlag(node, PATH_STORE_NODE_FLAG_ROOT)) {
    return setCachedNodePath(state, nodeId, "");
  }

  const parentPath = materializeNodePath(state, node.parentId);
  const nodeName = getSegmentValue(state.snapshot.segmentTable, node.nameId);
  const path = parentPath.length === 0 ? nodeName : `${parentPath}${nodeName}`;
  return setCachedNodePath(state, nodeId, isDirectoryNode(node) ? `${path}/` : path);
}

export function recomputeCountsUpwardFrom(state: PathStoreState, startNodeId: NodeId): void {
  recomputeCountsUpwardFromNow(state, startNodeId);
}

// Iterative post-order traversal that recomputes subtree and visible counts
// bottom-up. Uses an explicit stack to avoid recursive function-call overhead
// while preserving tree-order traversal for cache locality.
export function recomputeCountsRecursive(state: PathStoreState, nodeId: NodeId): void {
  // Stack frames: [nodeId, childOffset].  When childOffset equals the child
  // count, all children have been processed and we recompute this node.
  const stack: [NodeId, number][] = [[nodeId, 0]];
  const { nodes, directories } = state.snapshot;

  while (stack.length > 0) {
    const frame = stack[stack.length - 1];
    const nid = frame[0];
    const node = nodes[nid];

    if (node == null || !isDirectoryNode(node)) {
      // File or unknown — recompute immediately and pop.
      recomputeNodeCounts(state, nid, node, true);
      stack.pop();
      continue;
    }

    const dirIndex = directories.get(nid);
    if (dirIndex == null || frame[1] >= dirIndex.childIds.length) {
      // All children processed — recompute this directory and pop.
      recomputeNodeCounts(state, nid, node, true);
      stack.pop();
      continue;
    }

    // Push next child for processing.
    const childId = dirIndex.childIds[frame[1]++];
    stack.push([childId, 0]);
  }
}

export function collectAncestorIds(state: PathStoreState, nodeId: NodeId): NodeId[] {
  const ancestorIds: NodeId[] = [];
  let currentNodeId: NodeId | null = nodeId;

  while (currentNodeId != null) {
    const currentNode = requireNode(state, currentNodeId);
    ancestorIds.push(currentNodeId);
    if (currentNodeId === state.snapshot.rootId) {
      break;
    }

    currentNodeId = currentNode.parentId;
  }

  return ancestorIds;
}

export function findNodeId(state: PathStoreState, path: string): NodeId | null {
  if (path.length === 0) {
    return state.snapshot.rootId;
  }

  const lookupPath = parseLookupPath(path);
  return findNodeIdBySegments(state, lookupPath.segments, lookupPath.requiresDirectory);
}

function findNodeIdBySegments(
  state: PathStoreState,
  segments: readonly string[],
  requireDirectory: boolean,
): NodeId | null {
  let currentNodeId = state.snapshot.rootId;

  for (const segment of segments) {
    const segmentId = state.snapshot.segmentTable.idByValue.get(segment);
    if (segmentId === undefined) {
      return null;
    }

    // A path through a file names nothing, as when a selected file's folder became a file.
    if (!isDirectoryNode(requireNode(state, currentNodeId))) {
      return null;
    }
    const currentIndex = getDirectoryIndex(state, currentNodeId);
    const nextNodeId = ensureChildIdByNameId(state.snapshot.nodes, currentIndex).get(segmentId);
    if (nextNodeId === undefined) {
      return null;
    }

    currentNodeId = nextNodeId;
  }

  const currentNode = requireNode(state, currentNodeId);
  if (requireDirectory && !isDirectoryNode(currentNode)) {
    return null;
  }

  return currentNodeId;
}

export function getDirectoryIndex(state: PathStoreState, directoryId: NodeId): DirectoryChildIndex {
  const directoryIndex = state.snapshot.directories.get(directoryId);
  if (directoryIndex === undefined) {
    throw new Error(`Unknown directory child index for node ${String(directoryId)}`);
  }

  return directoryIndex;
}

export function requireNode(state: PathStoreState, nodeId: NodeId): PathStoreNode {
  const node = state.snapshot.nodes[nodeId];
  if (node === undefined) {
    throw new Error(`Unknown node ID: ${String(nodeId)}`);
  }

  return node;
}

// Canonical list output only includes files and explicit empty directories so
// the result can round-trip back into an equivalent store.
function collectCanonicalEntries(state: PathStoreState, nodeId: NodeId): string[] {
  const rootNode = state.snapshot.nodes[nodeId];
  if (rootNode === undefined) {
    return [];
  }

  if (!isDirectoryNode(rootNode)) {
    return [materializeNodePath(state, nodeId)];
  }

  if (getDirectoryIndex(state, nodeId).childIds.length === 0) {
    return hasNodeFlag(rootNode, PATH_STORE_NODE_FLAG_EXPLICIT) &&
      !hasNodeFlag(rootNode, PATH_STORE_NODE_FLAG_ROOT)
      ? [materializeNodePath(state, nodeId)]
      : [];
  }

  const entries: string[] = [];

  const stack: { childIndex: number; nodeId: NodeId }[] = [{ childIndex: 0, nodeId }];
  while (stack.length > 0) {
    const frame = stack[stack.length - 1];
    if (frame == null) {
      break;
    }

    const currentNode = state.snapshot.nodes[frame.nodeId];
    if (currentNode === undefined) {
      stack.pop();
      continue;
    }

    if (!isDirectoryNode(currentNode)) {
      entries.push(materializeNodePath(state, frame.nodeId));
      stack.pop();
      continue;
    }

    const currentIndex = getDirectoryIndex(state, frame.nodeId);
    if (currentIndex.childIds.length === 0) {
      if (
        hasNodeFlag(currentNode, PATH_STORE_NODE_FLAG_EXPLICIT) &&
        !hasNodeFlag(currentNode, PATH_STORE_NODE_FLAG_ROOT)
      ) {
        entries.push(materializeNodePath(state, frame.nodeId));
      }

      stack.pop();
      continue;
    }

    const nextChildId = currentIndex.childIds[frame.childIndex];
    if (nextChildId == null) {
      stack.pop();
      continue;
    }

    frame.childIndex++;
    stack.push({ childIndex: 0, nodeId: nextChildId });
  }

  return entries;
}

function ensureDirectoryChain(
  state: PathStoreState,
  directorySegments: readonly string[],
): { createdNodeIds: NodeId[]; directoryId: NodeId } {
  const createdNodeIds: NodeId[] = [];
  let currentDirectoryId = state.snapshot.rootId;

  for (const segment of directorySegments) {
    const segmentId = internSegment(state.snapshot.segmentTable, segment);
    const currentIndex = getDirectoryIndex(state, currentDirectoryId);
    const existingChildId = ensureChildIdByNameId(state.snapshot.nodes, currentIndex).get(
      segmentId,
    );

    if (existingChildId !== undefined) {
      const existingChild = requireNode(state, existingChildId);
      if (!isDirectoryNode(existingChild)) {
        throw new Error(
          `Cannot create a directory that collides with an existing file: "${segment}"`,
        );
      }

      currentDirectoryId = existingChildId;
      continue;
    }

    currentDirectoryId = createDirectoryNode(state, currentDirectoryId, segmentId);
    createdNodeIds.push(currentDirectoryId);
  }

  return { createdNodeIds, directoryId: currentDirectoryId };
}

function createDirectoryNode(state: PathStoreState, parentId: NodeId, nameId: number): NodeId {
  const parentNode = requireNode(state, parentId);
  const nodeId = state.snapshot.nodes.length;
  state.snapshot.nodes.push({
    depthAndFlags: createNodeDepthAndFlags(
      getNodeDepth(parentNode) + 1,
      0,
      PATH_STORE_NODE_KIND_DIRECTORY,
    ),
    nameId,
    parentId,
    subtreeNodeCount: 1,
    visibleSubtreeCount: 1,
  });
  state.snapshot.directories.set(nodeId, createDirectoryChildIndex());
  insertChildReference(state, parentId, nodeId);
  state.activeNodeCount++;
  return nodeId;
}

function createFileNode(state: PathStoreState, parentId: NodeId, basename: string): NodeId {
  const nameId = internSegment(state.snapshot.segmentTable, basename);
  const parentIndex = getDirectoryIndex(state, parentId);
  if (ensureChildIdByNameId(state.snapshot.nodes, parentIndex).has(nameId)) {
    throw new Error(`Path already exists: "${buildPathPreview(state, parentId, basename)}"`);
  }

  const parentNode = requireNode(state, parentId);
  const nodeId = state.snapshot.nodes.length;
  state.snapshot.nodes.push({
    depthAndFlags: createNodeDepthAndFlags(getNodeDepth(parentNode) + 1, 0),
    nameId,
    parentId,
    subtreeNodeCount: 1,
    visibleSubtreeCount: 1,
  });

  insertChildReference(state, parentId, nodeId);
  state.activeNodeCount++;
  return nodeId;
}

function findChildInsertIndex(
  state: PathStoreState,
  parentIndex: DirectoryChildIndex,
  childId: NodeId,
): number {
  let low = 0;
  let high = parentIndex.childIds.length;

  while (low < high) {
    const middle = (low + high) >>> 1;
    const existingChildId = parentIndex.childIds[middle];
    if (existingChildId == null) {
      high = middle;
      continue;
    }

    if (compareSiblingNodes(state, childId, existingChildId) < 0) {
      high = middle;
    } else {
      low = middle + 1;
    }
  }

  return low;
}

function insertChildReference(state: PathStoreState, parentId: NodeId, childId: NodeId): void {
  const parentIndex = getDirectoryIndex(state, parentId);
  const childNode = requireNode(state, childId);
  ensureChildIdByNameId(state.snapshot.nodes, parentIndex).set(childNode.nameId, childId);
  applyChildAggregateDelta(
    parentIndex,
    childId,
    childNode.subtreeNodeCount,
    childNode.visibleSubtreeCount,
  );

  const insertIndex = findChildInsertIndex(state, parentIndex, childId);
  parentIndex.childIds.splice(insertIndex, 0, childId);
  updateChildPositionsFrom(parentIndex, insertIndex);
  rebuildVisibleChildChunks(state.snapshot.nodes, parentIndex);
}

function compareSiblingNodes(state: PathStoreState, leftId: NodeId, rightId: NodeId): number {
  const sortOption = state.snapshot.options.sort;
  if (sortOption === "default") {
    return compareSiblingNodesDefault(state, leftId, rightId);
  }

  return sortOption(createCompareEntry(state, leftId), createCompareEntry(state, rightId));
}

function compareSiblingNodesDefault(
  state: PathStoreState,
  leftId: NodeId,
  rightId: NodeId,
): number {
  const leftNode = requireNode(state, leftId);
  const rightNode = requireNode(state, rightId);

  const leftIsDirectory = isDirectoryNode(leftNode);
  const rightIsDirectory = isDirectoryNode(rightNode);
  if (leftIsDirectory !== rightIsDirectory) {
    return leftIsDirectory ? -1 : 1;
  }

  const leftSortKey = getSegmentSortKey(state.snapshot.segmentTable, leftNode.nameId);
  const rightSortKey = getSegmentSortKey(state.snapshot.segmentTable, rightNode.nameId);
  const comparison = compareSegmentSortKeys(leftSortKey, rightSortKey);
  if (comparison !== 0) {
    return comparison;
  }

  const leftName = getSegmentValue(state.snapshot.segmentTable, leftNode.nameId);
  const rightName = getSegmentValue(state.snapshot.segmentTable, rightNode.nameId);
  if (leftName !== rightName) {
    return leftName < rightName ? -1 : 1;
  }

  return leftId < rightId ? -1 : 1;
}

function createCompareEntry(state: PathStoreState, nodeId: NodeId): PathStoreCompareEntry {
  const node = requireNode(state, nodeId);
  const path = materializeNodePath(state, nodeId);
  const isDirectory = isDirectoryNode(node);
  const normalizedPath = isDirectory ? path.slice(0, -1) : path;

  return {
    basename: getSegmentValue(state.snapshot.segmentTable, node.nameId),
    depth: getNodeDepth(node),
    isDirectory,
    path,
    segments: normalizedPath.length === 0 ? [] : normalizedPath.split("/"),
  };
}

function findDeepestExistingDirectoryId(
  state: PathStoreState,
  segments: readonly string[],
): NodeId {
  let currentDirectoryId = state.snapshot.rootId;

  for (const segment of segments) {
    const segmentId = state.snapshot.segmentTable.idByValue.get(segment);
    if (segmentId == null) {
      break;
    }

    const nextNodeId = ensureChildIdByNameId(
      state.snapshot.nodes,
      getDirectoryIndex(state, currentDirectoryId),
    ).get(segmentId);
    if (nextNodeId == null) {
      break;
    }

    const nextNode = requireNode(state, nextNodeId);
    if (!isDirectoryNode(nextNode)) {
      break;
    }

    currentDirectoryId = nextNodeId;
  }

  return currentDirectoryId;
}

function getCollapsedProjectionSignature(
  state: PathStoreState,
  startDirectoryId: NodeId,
): string | null {
  const collapsedAncestorId = findNearestCollapsedAncestor(state, startDirectoryId);
  if (collapsedAncestorId == null) {
    return null;
  }

  const terminalDirectoryId = getFlattenedTerminalDirectoryId(state, collapsedAncestorId);
  const terminalNode = requireNode(state, terminalDirectoryId);
  const flattenedSegmentPaths =
    collapsedAncestorId === terminalDirectoryId
      ? null
      : collectFlattenedDirectoryChainIds(state, collapsedAncestorId).map((nodeId) =>
          materializeNodePath(state, nodeId),
        );

  return JSON.stringify({
    flattenedSegmentPaths,
    hasChildren: getDirectoryIndex(state, terminalDirectoryId).childIds.length > 0,
    path: materializeNodePath(state, terminalDirectoryId),
    terminalKind: getNodeKind(terminalNode),
  });
}

function didProjectionChange(
  previousProjectionSignature: string | null,
  nextProjectionSignature: string | null,
): boolean {
  return didAnyProjectionChange([previousProjectionSignature], [nextProjectionSignature]);
}

function didAnyProjectionChange(
  previousProjectionSignatures: readonly (string | null)[],
  nextProjectionSignatures: readonly (string | null)[],
): boolean {
  for (let index = 0; index < previousProjectionSignatures.length; index += 1) {
    const previousProjectionSignature = previousProjectionSignatures[index];
    const nextProjectionSignature = nextProjectionSignatures[index];
    if (
      previousProjectionSignature == null ||
      nextProjectionSignature == null ||
      previousProjectionSignature !== nextProjectionSignature
    ) {
      return true;
    }
  }

  return false;
}

function findNearestCollapsedAncestor(
  state: PathStoreState,
  startDirectoryId: NodeId,
): NodeId | null {
  let currentDirectoryId: NodeId | null = startDirectoryId;

  while (currentDirectoryId != null) {
    const currentNode = requireNode(state, currentDirectoryId);
    if (!isDirectoryNode(currentNode) || hasNodeFlag(currentNode, PATH_STORE_NODE_FLAG_ROOT)) {
      return null;
    }

    if (!isDirectoryExpanded(state, currentDirectoryId, currentNode)) {
      return currentDirectoryId;
    }

    currentDirectoryId = currentNode.parentId;
  }

  return null;
}

function recomputeNodeCounts(
  state: PathStoreState,
  nodeId: NodeId,
  currentNode = requireNode(state, nodeId),
  rebuildChildAggregates = false,
): void {
  recomputeNodeCountsNow(state, nodeId, currentNode, rebuildChildAggregates);
}

function recomputeCountsUpwardFromNow(state: PathStoreState, startNodeId: NodeId): void {
  let currentNodeId: NodeId | null = startNodeId;

  while (currentNodeId != null) {
    const currentNode = requireNode(state, currentNodeId);
    const previousSubtreeNodeCount = currentNode.subtreeNodeCount;
    const previousVisibleSubtreeCount = currentNode.visibleSubtreeCount;
    recomputeNodeCounts(state, currentNodeId, currentNode);

    if (currentNodeId === state.snapshot.rootId) {
      return;
    }

    const subtreeNodeDelta = currentNode.subtreeNodeCount - previousSubtreeNodeCount;
    const visibleSubtreeDelta = currentNode.visibleSubtreeCount - previousVisibleSubtreeCount;
    const parentId = currentNode.parentId;

    if (subtreeNodeDelta !== 0 || visibleSubtreeDelta !== 0) {
      applyChildAggregateDelta(
        getDirectoryIndex(state, parentId),
        currentNodeId,
        subtreeNodeDelta,
        visibleSubtreeDelta,
      );
    }

    currentNodeId = parentId;
  }
}

// Recomputes one node's stored subtree counts from its cached child aggregates.
function recomputeNodeCountsNow(
  state: PathStoreState,
  nodeId: NodeId,
  currentNode: PathStoreNode,
  rebuildChildAggregates: boolean,
): void {
  if (!isDirectoryNode(currentNode)) {
    currentNode.subtreeNodeCount = 1;
    currentNode.visibleSubtreeCount = 1;
    return;
  }

  const currentIndex = getDirectoryIndex(state, nodeId);
  if (rebuildChildAggregates) {
    rebuildDirectoryChildAggregates(state.snapshot.nodes, currentIndex);
  }
  const subtreeNodeCount = 1 + currentIndex.totalChildSubtreeNodeCount;
  const visibleChildCount = currentIndex.totalChildVisibleSubtreeCount;

  currentNode.subtreeNodeCount = subtreeNodeCount;
  if (hasNodeFlag(currentNode, PATH_STORE_NODE_FLAG_ROOT)) {
    currentNode.visibleSubtreeCount = visibleChildCount;
    return;
  }

  const flattenedChildDirectoryId = getFlattenedChildDirectoryId(state, nodeId);
  currentNode.visibleSubtreeCount =
    flattenedChildDirectoryId != null
      ? visibleChildCount
      : isDirectoryExpanded(state, nodeId, currentNode)
        ? 1 + visibleChildCount
        : 1;
}

function buildPathPreview(state: PathStoreState, parentId: NodeId, basename: string): string {
  const parentPath = materializeNodePath(state, parentId);
  return parentPath.length === 0 ? basename : `${parentPath}${basename}`;
}
