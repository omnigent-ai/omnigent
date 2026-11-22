import {
  collectAncestorIds,
  findNodeId,
  getDirectoryIndex,
  materializeNodePath,
  recomputeCountsUpwardFrom,
  requireNode,
} from "./canonical";
import {
  ensureChildPositions,
  getVisibleChildPrefixCount,
  selectChildIndexByVisibleIndex,
} from "./child-index";
import { createCollapseEvent, createExpandEvent } from "./events";
import {
  collectFlattenedDirectoryChainIds,
  getFlattenedChildDirectoryId,
  getFlattenedTerminalDirectoryId,
} from "./flatten";
import type { DirectoryChildIndex, NodeId } from "./internal-types";
import { isDirectoryNode } from "./internal-types";
import type {
  PathStoreCollapseEvent,
  PathStoreExpandEvent,
  PathStoreVisibleAncestorRow,
  PathStoreVisibleRow,
  PathStoreVisibleRowContext,
  PathStoreVisibleTreeProjectionData,
} from "./public-types";
import { getSegmentValue } from "./segments";
import { isDirectoryExpanded, setDirectoryExpanded } from "./state";
import type { PathStoreState } from "./state";

const INITIAL_PROJECTION_DEPTH_CAPACITY = 64;
type ProjectionDepthTable = Int32Array<ArrayBufferLike>;

interface VisibleRowCursor {
  headNodeId: NodeId;
  terminalNodeId: NodeId;
  visibleDepth: number;
}

function ensureProjectionDepthCapacity(
  depthTable: ProjectionDepthTable,
  depth: number,
): ProjectionDepthTable {
  const requiredLength = depth + 2;
  if (requiredLength <= depthTable.length) {
    return depthTable;
  }

  let nextLength = depthTable.length;
  while (nextLength < requiredLength) {
    nextLength *= 2;
  }

  const nextDepthTable = new Int32Array(nextLength);
  nextDepthTable.fill(-1);
  nextDepthTable.set(depthTable);
  return nextDepthTable;
}

export function getVisibleCount(state: PathStoreState): number {
  return requireNode(state, state.snapshot.rootId).visibleSubtreeCount;
}

interface VisibleRowCursorWithIndex {
  cursor: VisibleRowCursor;
  index: number;
  posInSet: number;
  setSize: number;
}

function getVisibleRowSubtreeEndIndex(
  state: PathStoreState,
  cursor: VisibleRowCursor,
  index: number,
  totalVisibleCount: number,
): number {
  const terminalNode = requireNode(state, cursor.terminalNodeId);
  const subtreeSize = Math.max(1, terminalNode.visibleSubtreeCount);
  return Math.min(totalVisibleCount - 1, index + subtreeSize - 1);
}

function materializeVisibleAncestorRow(
  state: PathStoreState,
  entry: VisibleRowCursorWithIndex,
  totalVisibleCount: number,
  ancestorPaths: readonly string[],
): PathStoreVisibleAncestorRow {
  return {
    ancestorPaths,
    index: entry.index,
    posInSet: entry.posInSet,
    row: materializeVisibleRow(state, entry.cursor),
    setSize: entry.setSize,
    subtreeEndIndex: getVisibleRowSubtreeEndIndex(
      state,
      entry.cursor,
      entry.index,
      totalVisibleCount,
    ),
  };
}

function selectVisibleRowContextWithinDirectory(
  state: PathStoreState,
  directoryNodeId: NodeId,
  index: number,
  directoryStartIndex: number,
  parentVisibleDepth: number,
  ancestors: readonly VisibleRowCursorWithIndex[],
): {
  ancestors: readonly VisibleRowCursorWithIndex[];
  cursor: VisibleRowCursor;
  index: number;
  posInSet: number;
  setSize: number;
} {
  const directoryIndex = getDirectoryIndex(state, directoryNodeId);
  const { childIndex, childVisibleIndex, localVisibleIndex } = selectChildIndexByVisibleIndex(
    state.snapshot.nodes,
    directoryIndex,
    index,
  );
  const childId = directoryIndex.childIds[childIndex];
  if (childId == null) {
    throw new Error(`Visible index ${String(index)} is out of range`);
  }

  return selectVisibleRowContextWithinSubtree(
    state,
    childId,
    localVisibleIndex,
    directoryStartIndex + childVisibleIndex,
    parentVisibleDepth + 1,
    childIndex,
    directoryIndex.childIds.length,
    ancestors,
  );
}

function selectVisibleRowContextWithinSubtree(
  state: PathStoreState,
  nodeId: NodeId,
  index: number,
  rowIndex: number,
  visibleDepth: number,
  posInSet: number,
  setSize: number,
  ancestors: readonly VisibleRowCursorWithIndex[],
): {
  ancestors: readonly VisibleRowCursorWithIndex[];
  cursor: VisibleRowCursor;
  index: number;
  posInSet: number;
  setSize: number;
} {
  const node = requireNode(state, nodeId);
  if (!isDirectoryNode(node)) {
    if (index === 0) {
      return {
        ancestors,
        cursor: {
          headNodeId: nodeId,
          terminalNodeId: nodeId,
          visibleDepth,
        },
        index: rowIndex,
        posInSet,
        setSize,
      };
    }

    throw new Error(`Visible index ${String(index)} is out of range for file`);
  }

  const currentCursor = createVisibleRowCursor(state, nodeId, visibleDepth);
  if (index === 0) {
    return {
      ancestors,
      cursor: currentCursor,
      index: rowIndex,
      posInSet,
      setSize,
    };
  }

  const terminalNode = requireNode(state, currentCursor.terminalNodeId);
  if (
    !isDirectoryNode(terminalNode) ||
    !isDirectoryExpanded(state, currentCursor.terminalNodeId, terminalNode)
  ) {
    throw new Error(`Visible index ${String(index)} is out of range for collapsed directory`);
  }

  return selectVisibleRowContextWithinDirectory(
    state,
    currentCursor.terminalNodeId,
    index - 1,
    rowIndex + 1,
    currentCursor.visibleDepth,
    [...ancestors, { cursor: currentCursor, index: rowIndex, posInSet, setSize }],
  );
}

export function getVisibleRowContext(
  state: PathStoreState,
  index: number,
): PathStoreVisibleRowContext | null {
  const totalVisibleCount = getVisibleCount(state);
  if (index < 0 || index >= totalVisibleCount) {
    return null;
  }

  const selected = selectVisibleRowContextWithinDirectory(
    state,
    state.snapshot.rootId,
    index,
    0,
    -1,
    [],
  );
  const ancestorPaths = selected.ancestors.map((ancestor) =>
    materializeNodePath(state, ancestor.cursor.terminalNodeId),
  );
  let cachedAncestorRows: readonly PathStoreVisibleAncestorRow[] | null = null;

  return {
    ancestorPaths,
    get ancestorRows() {
      if (cachedAncestorRows != null) {
        return cachedAncestorRows;
      }

      const ancestorRows: PathStoreVisibleAncestorRow[] = [];
      const rowAncestorPaths: string[] = [];
      for (const ancestor of selected.ancestors) {
        const ancestorRow = materializeVisibleAncestorRow(state, ancestor, totalVisibleCount, [
          ...rowAncestorPaths,
        ]);
        ancestorRows.push(ancestorRow);
        rowAncestorPaths.push(ancestorRow.row.path);
      }
      cachedAncestorRows = ancestorRows;
      return cachedAncestorRows;
    },
    index: selected.index,
    posInSet: selected.posInSet,
    row: materializeVisibleRow(state, selected.cursor),
    setSize: selected.setSize,
    subtreeEndIndex: getVisibleRowSubtreeEndIndex(
      state,
      selected.cursor,
      selected.index,
      totalVisibleCount,
    ),
  };
}

export function getVisibleSlice(
  state: PathStoreState,
  start: number,
  end: number,
): readonly PathStoreVisibleRow[] {
  const totalVisibleCount = getVisibleCount(state);
  if (totalVisibleCount <= 0 || end < start) {
    return [];
  }

  const normalizedStart = Math.max(0, Math.min(start, totalVisibleCount - 1));
  const normalizedEnd = Math.max(normalizedStart, Math.min(end, totalVisibleCount - 1));

  // Fast path: full-tree DFS avoids the expensive parent-walk for finding
  // next siblings that getNextVisibleRowCursor performs.
  if (normalizedStart === 0) {
    return collectVisibleRowsDFS(state, normalizedEnd + 1);
  }

  const rows: PathStoreVisibleRow[] = [];
  let currentCursor = selectVisibleRow(state, normalizedStart);

  for (
    let visibleIndex = normalizedStart;
    visibleIndex <= normalizedEnd && currentCursor != null;
    visibleIndex++
  ) {
    const row = materializeVisibleRow(state, currentCursor);
    rows.push(row);
    currentCursor = getNextVisibleRowCursor(state, currentCursor);
  }

  return rows;
}

export function getVisibleTreeProjectionData(
  state: PathStoreState,
  maxRows: number = getVisibleCount(state),
): PathStoreVisibleTreeProjectionData {
  return buildVisibleTreeProjectionDataDFS(state, maxRows);
}

// Resolves one canonical path to its current visible row index using stored
// subtree counts rather than materializing the whole path-to-index map.
export function getVisibleIndexByPath(state: PathStoreState, path: string): number | null {
  const nodeId = findNodeId(state, path);
  if (nodeId == null || nodeId === state.snapshot.rootId) {
    return null;
  }

  const node = requireNode(state, nodeId);
  if (isDirectoryNode(node) && getFlattenedTerminalDirectoryId(state, nodeId) !== nodeId) {
    return null;
  }

  let visibleIndex = 0;
  let currentNodeId = nodeId;
  const { nodes, rootId } = state.snapshot;

  while (currentNodeId !== rootId) {
    const currentNode = requireNode(state, currentNodeId);
    const parentId = currentNode.parentId;
    const parentIndex = getDirectoryIndex(state, parentId);
    const childPosition = ensureChildPositions(parentIndex).get(currentNodeId);
    if (childPosition == null) {
      throw new Error(`Child ${String(currentNodeId)} was not found in its parent index`);
    }

    visibleIndex += getVisibleChildPrefixCount(nodes, parentIndex, childPosition);

    if (parentId !== rootId) {
      const parentNode = requireNode(state, parentId);
      const flattenedChildDirectoryId = getFlattenedChildDirectoryId(state, parentId);
      if (
        !isDirectoryExpanded(state, parentId, parentNode) &&
        flattenedChildDirectoryId !== currentNodeId
      ) {
        return null;
      }

      if (getFlattenedTerminalDirectoryId(state, parentId) === parentId) {
        visibleIndex += 1;
      }
    }

    currentNodeId = parentId;
  }

  return visibleIndex;
}

export function expandPath(state: PathStoreState, path: string): PathStoreExpandEvent | null {
  const directoryNodeId = findNodeId(state, path);
  if (directoryNodeId == null) {
    throw new Error(`Path does not exist: "${path}"`);
  }

  const directoryNode = requireNode(state, directoryNodeId);
  if (!isDirectoryNode(directoryNode)) {
    throw new Error(`Path is not a directory: "${path}"`);
  }

  if (isDirectoryExpanded(state, directoryNodeId, directoryNode)) {
    return null;
  }

  setDirectoryExpanded(state, directoryNodeId, true, directoryNode);
  recomputeCountsUpwardFrom(state, directoryNodeId);
  return createExpandEvent({
    affectedAncestorIds: collectAncestorIds(state, directoryNodeId),
    affectedNodeIds: [directoryNodeId],
    path,
    projectionChanged: true,
  });
}

export function collapsePath(state: PathStoreState, path: string): PathStoreCollapseEvent | null {
  const directoryNodeId = findNodeId(state, path);
  if (directoryNodeId == null) {
    throw new Error(`Path does not exist: "${path}"`);
  }

  const directoryNode = requireNode(state, directoryNodeId);
  if (!isDirectoryNode(directoryNode)) {
    throw new Error(`Path is not a directory: "${path}"`);
  }

  if (!isDirectoryExpanded(state, directoryNodeId, directoryNode)) {
    return null;
  }

  setDirectoryExpanded(state, directoryNodeId, false, directoryNode);
  recomputeCountsUpwardFrom(state, directoryNodeId);
  return createCollapseEvent({
    affectedAncestorIds: collectAncestorIds(state, directoryNodeId),
    affectedNodeIds: [directoryNodeId],
    path,
    projectionChanged: true,
  });
}

function selectVisibleRow(state: PathStoreState, index: number): VisibleRowCursor | null {
  if (index < 0 || index >= getVisibleCount(state)) {
    return null;
  }

  return selectVisibleRowWithinDirectory(state, state.snapshot.rootId, index, -1);
}

function selectVisibleRowWithinDirectory(
  state: PathStoreState,
  directoryNodeId: NodeId,
  index: number,
  parentVisibleDepth: number,
): VisibleRowCursor {
  const directoryIndex = getDirectoryIndex(state, directoryNodeId);
  const { childIndex, localVisibleIndex } = selectChildIndexByVisibleIndex(
    state.snapshot.nodes,
    directoryIndex,
    index,
  );
  const childId = directoryIndex.childIds[childIndex];
  if (childId != null) {
    return selectVisibleRowWithinSubtree(state, childId, localVisibleIndex, parentVisibleDepth + 1);
  }

  throw new Error(`Visible index ${String(index)} is out of range`);
}

function selectVisibleRowWithinSubtree(
  state: PathStoreState,
  nodeId: NodeId,
  index: number,
  visibleDepth: number,
): VisibleRowCursor {
  const node = requireNode(state, nodeId);
  if (!isDirectoryNode(node)) {
    if (index === 0) {
      return {
        headNodeId: nodeId,
        terminalNodeId: nodeId,
        visibleDepth,
      };
    }

    throw new Error(`Visible index ${String(index)} is out of range for file`);
  }

  const currentCursor = createVisibleRowCursor(state, nodeId, visibleDepth);
  if (index === 0) {
    return currentCursor;
  }

  const terminalNode = requireNode(state, currentCursor.terminalNodeId);
  if (
    !isDirectoryNode(terminalNode) ||
    !isDirectoryExpanded(state, currentCursor.terminalNodeId, terminalNode)
  ) {
    throw new Error(`Visible index ${String(index)} is out of range for collapsed directory`);
  }

  return selectVisibleRowWithinDirectory(
    state,
    currentCursor.terminalNodeId,
    index - 1,
    currentCursor.visibleDepth,
  );
}

function createVisibleRowCursor(
  state: PathStoreState,
  nodeId: NodeId,
  visibleDepth: number,
): VisibleRowCursor {
  const node = requireNode(state, nodeId);
  if (!isDirectoryNode(node)) {
    return {
      headNodeId: nodeId,
      terminalNodeId: nodeId,
      visibleDepth,
    };
  }

  return {
    headNodeId: nodeId,
    terminalNodeId: getFlattenedTerminalDirectoryId(state, nodeId),
    visibleDepth,
  };
}

function isVisibleRowHeadNode(state: PathStoreState, nodeId: NodeId): boolean {
  const node = requireNode(state, nodeId);
  if (!isDirectoryNode(node)) {
    return true;
  }

  const parentId = node.parentId;
  if (parentId === state.snapshot.rootId) {
    return true;
  }

  return getFlattenedChildDirectoryId(state, parentId) !== nodeId;
}

// Walks the visible preorder sequence without materializing the full row list.
function getNextVisibleRowCursor(
  state: PathStoreState,
  currentCursor: VisibleRowCursor,
): VisibleRowCursor | null {
  const terminalNode = requireNode(state, currentCursor.terminalNodeId);
  if (isDirectoryNode(terminalNode)) {
    const currentIndex = getDirectoryIndex(state, currentCursor.terminalNodeId);
    if (
      isDirectoryExpanded(state, currentCursor.terminalNodeId, terminalNode) &&
      currentIndex.childIds.length > 0
    ) {
      const firstChildId = currentIndex.childIds[0];
      return firstChildId == null
        ? null
        : selectVisibleRowWithinSubtree(state, firstChildId, 0, currentCursor.visibleDepth + 1);
    }
  }

  let currentNodeId: NodeId = currentCursor.terminalNodeId;
  let currentVisibleDepth = currentCursor.visibleDepth;
  while (true) {
    const currentNode = requireNode(state, currentNodeId);
    if (currentNodeId === state.snapshot.rootId) {
      return null;
    }

    const parentId = currentNode.parentId;
    const parentIndex = getDirectoryIndex(state, parentId);
    const siblingIndex = ensureChildPositions(parentIndex).get(currentNodeId) ?? -1;
    if (siblingIndex < 0) {
      throw new Error(`Child ${String(currentNodeId)} was not found in its parent index`);
    }

    const nextSiblingId = parentIndex.childIds[siblingIndex + 1] ?? null;
    if (nextSiblingId != null) {
      return selectVisibleRowWithinSubtree(state, nextSiblingId, 0, currentVisibleDepth);
    }

    if (isVisibleRowHeadNode(state, currentNodeId)) {
      currentVisibleDepth--;
    }
    currentNodeId = parentId;
  }
}

// Walks the full visible preorder and builds the ARIA projection data directly
// into path and typed-array buffers so tree startup can avoid allocating a
// projection row object for every visible item.
function buildVisibleTreeProjectionDataDFS(
  state: PathStoreState,
  maxRows: number,
): PathStoreVisibleTreeProjectionData {
  const paths = new Array<string>(maxRows);
  const parentRowIndex = new Int32Array(maxRows);
  const posInSetByIndex = new Int32Array(maxRows);
  const setSizeByIndex = new Int32Array(maxRows);
  let lastRowAtDepth: ProjectionDepthTable = new Int32Array(INITIAL_PROJECTION_DEPTH_CAPACITY);
  lastRowAtDepth.fill(-1);

  let rowCount = 0;
  const { nodes, directories, segmentTable } = state.snapshot;
  const stack: [DirectoryChildIndex, number, number, string][] = [
    [directories.get(state.snapshot.rootId)!, 0, -1, ""],
  ];
  const flattenEnabled = state.snapshot.options.flattenEmptyDirectories;
  const pathCacheByNodeId = state.pathCacheByNodeId;
  const pathCacheVersion = state.pathCacheVersion;
  const segmentValues = segmentTable.valueById;

  while (stack.length > 0 && rowCount < maxRows) {
    const frame = stack[stack.length - 1];
    const dirIndex = frame[0];

    if (frame[1] >= dirIndex.childIds.length) {
      stack.pop();
      continue;
    }

    const childOffset = frame[1];
    const childId = dirIndex.childIds[frame[1]++];
    const childNode = nodes[childId];
    const visibleDepth = frame[2] + 1;
    const parentPath = frame[3];
    lastRowAtDepth = ensureProjectionDepthCapacity(lastRowAtDepth, visibleDepth);

    let path: string;
    let terminalNodeId = childId;
    if (!isDirectoryNode(childNode)) {
      const cachedPathEntry = pathCacheByNodeId.get(childId);
      path =
        cachedPathEntry != null && cachedPathEntry.version === pathCacheVersion
          ? cachedPathEntry.path
          : `${parentPath}${segmentValues[childNode.nameId]}`;
    } else {
      terminalNodeId = flattenEnabled ? getFlattenedTerminalDirectoryId(state, childId) : childId;
      path =
        terminalNodeId === childId
          ? `${parentPath}${segmentValues[childNode.nameId]}/`
          : materializeNodePath(state, terminalNodeId);
    }

    const parentIdx = lastRowAtDepth[visibleDepth];
    parentRowIndex[rowCount] = parentIdx;
    paths[rowCount] = path;
    posInSetByIndex[rowCount] = childOffset;
    // The current frame iterates the full child array for the row's parent, so
    // childIds.length stays correct even when we cap the emitted projection.
    setSizeByIndex[rowCount] = dirIndex.childIds.length;
    lastRowAtDepth[visibleDepth + 1] = rowCount;

    rowCount += 1;

    const terminalNode = nodes[terminalNodeId];
    if (
      terminalNode != null &&
      isDirectoryNode(terminalNode) &&
      isDirectoryExpanded(state, terminalNodeId, terminalNode)
    ) {
      stack.push([directories.get(terminalNodeId)!, 0, visibleDepth, path]);
    }
  }

  if (rowCount < maxRows) {
    paths.length = rowCount;
  }

  const finalParentRowIndex = parentRowIndex.subarray(0, rowCount);
  const finalPosInSetByIndex = posInSetByIndex.subarray(0, rowCount);
  const finalSetSizeByIndex = setSizeByIndex.subarray(0, rowCount);
  let cachedVisibleIndexByPath: Map<string, number> | null = null;
  return {
    getParentIndex(index: number): number {
      return index < 0 || index >= rowCount ? -1 : (finalParentRowIndex[index] ?? -1);
    },
    paths,
    posInSetByIndex: finalPosInSetByIndex,
    setSizeByIndex: finalSetSizeByIndex,
    get visibleIndexByPath(): Map<string, number> {
      if (cachedVisibleIndexByPath == null) {
        cachedVisibleIndexByPath = new Map<string, number>();
        for (let rowIndex = 0; rowIndex < rowCount; rowIndex += 1) {
          cachedVisibleIndexByPath.set(paths[rowIndex] ?? "", rowIndex);
        }
      }

      return cachedVisibleIndexByPath;
    },
  };
}

// Iterative depth-first traversal that collects visible rows by walking the
// child arrays directly.  This is faster than the cursor-based approach for
// large contiguous slices starting from index 0, because it never needs to
// walk up the tree to locate the next sibling.
function collectVisibleRowsDFS(state: PathStoreState, maxRows: number): PathStoreVisibleRow[] {
  // Pre-allocate output array to avoid dynamic resizing from push().
  const rows: PathStoreVisibleRow[] = new Array(maxRows);
  let rowCount = 0;
  // Stack frame: [directoryChildIndex, childOffset, visibleDepth]
  // Caching the DirectoryChildIndex directly avoids a Map.get per child
  // iteration — the lookup is done once when entering the directory.
  const { nodes, directories, segmentTable } = state.snapshot;
  const stack: [DirectoryChildIndex, number, number][] = [
    [directories.get(state.snapshot.rootId)!, 0, -1],
  ];
  const segmentValues = segmentTable.valueById;
  const flattenEnabled = state.snapshot.options.flattenEmptyDirectories;
  const pathCacheByNodeId = state.pathCacheByNodeId;
  const pathCacheVersion = state.pathCacheVersion;

  while (stack.length > 0 && rowCount < maxRows) {
    const frame = stack[stack.length - 1];
    const dirIndex = frame[0];

    if (frame[1] >= dirIndex.childIds.length) {
      stack.pop();
      continue;
    }

    const childId = dirIndex.childIds[frame[1]++];
    const childNode = nodes[childId];

    const visibleDepth = frame[2] + 1;

    if (!isDirectoryNode(childNode)) {
      // File node — inline materialization avoids cursor allocation and
      // directory-specific checks (load state, flattening, expansion).
      const cachedPathEntry = pathCacheByNodeId.get(childId);
      rows[rowCount++] = {
        depth: visibleDepth,
        flattenedSegments: undefined,
        hasChildren: false,
        id: childId,
        isExpanded: false,
        isFlattened: false,
        kind: "file",
        name: segmentValues[childNode.nameId],
        path:
          cachedPathEntry != null && cachedPathEntry.version === pathCacheVersion
            ? cachedPathEntry.path
            : materializeNodePath(state, childId),
      };
      continue;
    }

    // Directory node — delegate to materializeVisibleRow which correctly
    // handles load states, flattened chains, and all edge cases.
    const terminalNodeId = flattenEnabled
      ? getFlattenedTerminalDirectoryId(state, childId)
      : childId;
    const cursor: VisibleRowCursor = {
      headNodeId: childId,
      terminalNodeId,
      visibleDepth,
    };
    rows[rowCount++] = materializeVisibleRow(state, cursor);

    // Descend into expanded directories.
    const terminalNode = nodes[terminalNodeId];
    if (
      terminalNode != null &&
      isDirectoryNode(terminalNode) &&
      isDirectoryExpanded(state, terminalNodeId, terminalNode)
    ) {
      stack.push([directories.get(terminalNodeId)!, 0, visibleDepth]);
    }
  }

  if (rowCount < maxRows) {
    rows.length = rowCount;
  }

  return rows;
}

function materializeVisibleRow(
  state: PathStoreState,
  cursor: VisibleRowCursor,
): PathStoreVisibleRow {
  const terminalNode = requireNode(state, cursor.terminalNodeId);
  const path = materializeNodePath(state, cursor.terminalNodeId);
  const name = getSegmentValue(state.snapshot.segmentTable, terminalNode.nameId);
  const hasChildren =
    isDirectoryNode(terminalNode) &&
    getDirectoryIndex(state, cursor.terminalNodeId).childIds.length > 0;
  const isFlattened = cursor.headNodeId !== cursor.terminalNodeId;
  const flattenedSegments = isFlattened
    ? collectFlattenedDirectoryChainIds(state, cursor.headNodeId).map((nodeId) => {
        const node = requireNode(state, nodeId);
        return {
          isTerminal: nodeId === cursor.terminalNodeId,
          name: getSegmentValue(state.snapshot.segmentTable, node.nameId),
          nodeId,
          path: materializeNodePath(state, nodeId),
        };
      })
    : undefined;

  return {
    depth: cursor.visibleDepth,
    flattenedSegments,
    hasChildren,
    id: cursor.terminalNodeId,
    isExpanded:
      isDirectoryNode(terminalNode) &&
      isDirectoryExpanded(state, cursor.terminalNodeId, terminalNode),
    isFlattened,
    kind: isDirectoryNode(terminalNode) ? "directory" : "file",
    name,
    path,
  };
}
