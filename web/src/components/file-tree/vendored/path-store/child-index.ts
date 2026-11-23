import type { DirectoryChildIndex, NodeId, PathStoreNode, SegmentId } from "./internal-types";

const PATH_STORE_CHILD_INDEX_CHUNK_SHIFT = 5;
const PATH_STORE_CHILD_INDEX_CHUNK_SIZE = 1 << PATH_STORE_CHILD_INDEX_CHUNK_SHIFT;
const PATH_STORE_CHILD_INDEX_CHUNK_THRESHOLD = PATH_STORE_CHILD_INDEX_CHUNK_SIZE * 4;

// Exposed so hot callers can avoid the rebuildVisibleChildChunks function
// call for directories that fall below the chunk threshold (the vast
// majority on tree-shaped workloads).
export const PATH_STORE_CHILD_INDEX_CHUNK_THRESHOLD_EXTERNAL: number =
  PATH_STORE_CHILD_INDEX_CHUNK_THRESHOLD;

export function createDirectoryChildIndex(): DirectoryChildIndex {
  return {
    childIdByNameId: new Map<SegmentId, NodeId>(),
    childIds: [],
    childPositionById: new Map<NodeId, number>(),
    childVisibleChunkSums: null,
    totalChildSubtreeNodeCount: 0,
    totalChildVisibleSubtreeCount: 0,
  };
}

// Lazily rebuilds the child-name-id lookup map from the childIds array and
// the stored nameId on each node.
export function ensureChildIdByNameId(
  nodes: readonly PathStoreNode[],
  index: DirectoryChildIndex,
): Map<SegmentId, NodeId> {
  if (index.childIdByNameId != null) {
    return index.childIdByNameId;
  }

  const map = new Map<SegmentId, NodeId>();
  for (const childId of index.childIds) {
    const childNode = nodes[childId];
    if (childNode != null) {
      map.set(childNode.nameId, childId);
    }
  }

  index.childIdByNameId = map;
  return map;
}

export function ensureChildPositions(index: DirectoryChildIndex): Map<NodeId, number> {
  if (index.childPositionById != null) {
    return index.childPositionById;
  }

  const positions = new Map<NodeId, number>();
  for (let i = 0; i < index.childIds.length; i++) {
    const childId = index.childIds[i];
    if (childId != null) {
      positions.set(childId, i);
    }
  }

  index.childPositionById = positions;
  return positions;
}

export function appendChildReference(index: DirectoryChildIndex, childId: NodeId): void {
  if (index.childPositionById != null) {
    index.childPositionById.set(childId, index.childIds.length);
  }
  index.childIds.push(childId);
}

// Rebuilds the child-position map from the first changed index after a splice.
export function updateChildPositionsFrom(index: DirectoryChildIndex, startIndex: number): void {
  if (index.childPositionById == null) {
    return;
  }

  for (let position = startIndex; position < index.childIds.length; position++) {
    const childId = index.childIds[position];
    if (childId != null) {
      index.childPositionById.set(childId, position);
    }
  }
}

// Stores fast aggregate totals on each directory so ancestor count repair
// doesn't need to rescan every child after small edits.
export function rebuildDirectoryChildAggregates(
  nodes: readonly PathStoreNode[],
  index: DirectoryChildIndex,
): void {
  let totalChildSubtreeNodeCount = 0;
  let totalChildVisibleSubtreeCount = 0;

  for (const childId of index.childIds) {
    const childNode = nodes[childId];
    if (childNode == null) {
      continue;
    }

    totalChildSubtreeNodeCount += childNode.subtreeNodeCount;
    totalChildVisibleSubtreeCount += childNode.visibleSubtreeCount;
  }

  index.totalChildSubtreeNodeCount = totalChildSubtreeNodeCount;
  index.totalChildVisibleSubtreeCount = totalChildVisibleSubtreeCount;
  rebuildVisibleChildChunks(nodes, index);
}

export function applyChildAggregateDelta(
  index: DirectoryChildIndex,
  childId: NodeId,
  subtreeNodeDelta: number,
  visibleSubtreeDelta: number,
): void {
  index.totalChildSubtreeNodeCount += subtreeNodeDelta;
  index.totalChildVisibleSubtreeCount += visibleSubtreeDelta;

  if (index.childVisibleChunkSums == null || visibleSubtreeDelta === 0) {
    return;
  }

  const childPosition = ensureChildPositions(index).get(childId);
  if (childPosition === undefined) {
    return;
  }

  const chunkIndex = childPosition >> PATH_STORE_CHILD_INDEX_CHUNK_SHIFT;
  index.childVisibleChunkSums[chunkIndex] += visibleSubtreeDelta;
}

// Skips over wide child arrays by using chunked visible-count summaries first
// and then scanning only within the selected chunk.
export function selectChildIndexByVisibleIndex(
  nodes: readonly PathStoreNode[],
  index: DirectoryChildIndex,
  visibleIndex: number,
): {
  childIndex: number;
  childVisibleIndex: number;
  localVisibleIndex: number;
} {
  const chunkSums = index.childVisibleChunkSums;

  if (chunkSums != null) {
    let remainingIndex = visibleIndex;
    let childIndex = 0;
    for (const chunkVisibleCount of chunkSums) {
      if (remainingIndex < chunkVisibleCount) {
        const selected = selectChildIndexWithinChunk(nodes, index, childIndex, remainingIndex);
        return {
          ...selected,
          childVisibleIndex: visibleIndex - selected.localVisibleIndex,
        };
      }

      remainingIndex -= chunkVisibleCount;
      childIndex += PATH_STORE_CHILD_INDEX_CHUNK_SIZE;
    }

    throw new Error(`Visible child index ${String(visibleIndex)} is out of range`);
  }

  let remainingIndex = visibleIndex;
  for (let childIndex = 0; childIndex < index.childIds.length; childIndex++) {
    const childId = index.childIds[childIndex];
    if (childId == null) {
      continue;
    }

    const childNode = nodes[childId];
    if (childNode == null) {
      continue;
    }

    if (remainingIndex < childNode.visibleSubtreeCount) {
      return {
        childIndex,
        childVisibleIndex: visibleIndex - remainingIndex,
        localVisibleIndex: remainingIndex,
      };
    }

    remainingIndex -= childNode.visibleSubtreeCount;
  }

  throw new Error(`Visible child index ${String(visibleIndex)} is out of range`);
}

// Returns the number of visible rows contributed by siblings before a child.
// Wide directories use the same chunk sums as visible-index selection so path
// lookups do not need to scan thousands of siblings one by one.
export function getVisibleChildPrefixCount(
  nodes: readonly PathStoreNode[],
  index: DirectoryChildIndex,
  childPosition: number,
): number {
  let visibleCount = 0;
  const chunkSums = index.childVisibleChunkSums;
  let scanStart = 0;

  if (chunkSums != null) {
    const chunkIndex = childPosition >> PATH_STORE_CHILD_INDEX_CHUNK_SHIFT;
    for (let chunkOffset = 0; chunkOffset < chunkIndex; chunkOffset += 1) {
      visibleCount += chunkSums[chunkOffset] ?? 0;
    }
    scanStart = chunkIndex << PATH_STORE_CHILD_INDEX_CHUNK_SHIFT;
  }

  for (let childIndex = scanStart; childIndex < childPosition; childIndex += 1) {
    const childId = index.childIds[childIndex];
    if (childId == null) {
      continue;
    }

    const childNode = nodes[childId];
    if (childNode == null) {
      continue;
    }

    visibleCount += childNode.visibleSubtreeCount;
  }

  return visibleCount;
}

export function rebuildVisibleChildChunks(
  nodes: readonly PathStoreNode[],
  index: DirectoryChildIndex,
): void {
  if (index.childIds.length < PATH_STORE_CHILD_INDEX_CHUNK_THRESHOLD) {
    index.childVisibleChunkSums = null;
    return;
  }

  const chunkCount = Math.ceil(index.childIds.length / PATH_STORE_CHILD_INDEX_CHUNK_SIZE);
  const chunkSums = new Int32Array(chunkCount);

  for (let childIndex = 0; childIndex < index.childIds.length; childIndex++) {
    const childId = index.childIds[childIndex];
    if (childId == null) {
      continue;
    }

    const childNode = nodes[childId];
    if (childNode == null) {
      continue;
    }

    chunkSums[childIndex >> PATH_STORE_CHILD_INDEX_CHUNK_SHIFT] += childNode.visibleSubtreeCount;
  }

  index.childVisibleChunkSums = chunkSums;
}

function selectChildIndexWithinChunk(
  nodes: readonly PathStoreNode[],
  index: DirectoryChildIndex,
  chunkStartIndex: number,
  visibleIndex: number,
): { childIndex: number; localVisibleIndex: number } {
  const chunkEndIndex = Math.min(
    index.childIds.length,
    chunkStartIndex + PATH_STORE_CHILD_INDEX_CHUNK_SIZE,
  );
  let remainingIndex = visibleIndex;

  for (let childIndex = chunkStartIndex; childIndex < chunkEndIndex; childIndex++) {
    const childId = index.childIds[childIndex];
    if (childId == null) {
      continue;
    }

    const childNode = nodes[childId];
    if (childNode == null) {
      continue;
    }

    if (remainingIndex < childNode.visibleSubtreeCount) {
      return { childIndex, localVisibleIndex: remainingIndex };
    }

    remainingIndex -= childNode.visibleSubtreeCount;
  }

  throw new Error(`Visible child index ${String(visibleIndex)} is out of range`);
}
