import { PathStoreBuilder, preparePathEntries } from "./builder";
import {
  addPath,
  findNodeId,
  getDirectoryIndex,
  listPaths,
  materializeNodePath,
  recomputeCountsRecursive,
  requireNode,
} from "./canonical";
import {
  PATH_STORE_CHILD_INDEX_CHUNK_THRESHOLD_EXTERNAL,
  rebuildVisibleChildChunks,
} from "./child-index";
import { batchEvents, finalizeEvent, recordEvent, subscribe } from "./events";
import { getNodeDepth, isDirectoryNode } from "./internal-types";
import type { NodeId } from "./internal-types";
import {
  collapsePath,
  expandPath,
  getVisibleCount,
  getVisibleIndexByPath,
  getVisibleRowContext,
  getVisibleSlice,
  getVisibleTreeProjectionData as getVisibleTreeProjectionDataFromState,
} from "./projection";
import type {
  PathStoreConstructorOptions,
  PathStoreEventForType,
  PathStoreEventType,
  PathStorePathInfo,
  PathStoreVisibleRow,
  PathStoreVisibleRowContext,
  PathStoreVisibleTreeProjectionData,
} from "./public-types";
import { isDirectoryExpanded, setDirectoryExpanded, createPathStoreState } from "./state";
import type { PathStoreState } from "./state";

// Initializes the common all-directories-open startup shape without rerunning
// the generic count-repair walk the constructor uses for arbitrary expansion
// overrides.
function initializeOpenVisibleCounts(state: PathStoreState): void {
  const { directories, nodes, options, rootId } = state.snapshot;
  const flattenEmptyDirectories = options.flattenEmptyDirectories === true;

  // Iterative reverse-order walk processing directories in post-order.
  // Sorted construction assigns node IDs sequentially, so a directory's
  // descendants always have higher IDs than the directory itself. This means
  // reverse iteration finalizes every descendant before its parent.
  //
  // PathStore's constructor passes `skipSubtreeCountPass: true` to
  // builder.finish(), so subtreeNodeCount arrives un-accumulated (all 1s).
  // This walk writes both subtreeNodeCount and visibleSubtreeCount for
  // every directory; the reverse order guarantees children's counts are
  // already finalized before their parent reads them.
  // Root (nodeId === rootId === 0) is never passed to walkDirectory because
  // the loop stops at nodeId 1, so the walker needs no root check.
  const walkDirectory = (nodeId: NodeId): void => {
    const currentNode = nodes[nodeId];
    if (currentNode == null || !isDirectoryNode(currentNode)) {
      return;
    }

    const currentIndex = directories.get(nodeId);
    if (currentIndex == null) {
      throw new Error(`Unknown directory child index for node ${String(nodeId)}`);
    }

    const childIds = currentIndex.childIds;
    const childCount = childIds.length;
    let totalChildSubtreeNodeCount = 0;
    let totalChildVisibleSubtreeCount = 0;
    for (let ci = 0; ci < childCount; ci++) {
      const childId = childIds[ci];
      if (childId == null) {
        continue;
      }
      const childNode = nodes[childId];
      totalChildSubtreeNodeCount += childNode.subtreeNodeCount;
      totalChildVisibleSubtreeCount += childNode.visibleSubtreeCount;
    }

    currentIndex.totalChildSubtreeNodeCount = totalChildSubtreeNodeCount;
    currentIndex.totalChildVisibleSubtreeCount = totalChildVisibleSubtreeCount;
    // Avoid the rebuildVisibleChildChunks function call for directories that
    // don't need chunk sums. The threshold matches the internal constant;
    // new child indexes start with null childVisibleChunkSums, so directories
    // below the threshold need no write.
    if (childCount >= PATH_STORE_CHILD_INDEX_CHUNK_THRESHOLD_EXTERNAL) {
      rebuildVisibleChildChunks(nodes, currentIndex);
    }

    // The builder's backward subtree-count pass is skipped by PathStore, so
    // we populate subtreeNodeCount inline here. The reverse-order walk
    // guarantees children's subtreeNodeCount has already been written before
    // we read them.
    currentNode.subtreeNodeCount = 1 + totalChildSubtreeNodeCount;

    // Root is at nodeId 0 and is handled after the loop, so the root flag
    // never fires inside this body.
    let newVisibleSubtreeCount: number;
    if (flattenEmptyDirectories && childCount === 1) {
      // Flattened directories inherit their sole child's visible count
      // rather than contributing their own header row.
      const onlyChild = nodes[childIds[0]];
      newVisibleSubtreeCount =
        onlyChild != null && isDirectoryNode(onlyChild)
          ? totalChildVisibleSubtreeCount
          : 1 + totalChildVisibleSubtreeCount;
    } else {
      newVisibleSubtreeCount = 1 + totalChildVisibleSubtreeCount;
    }

    currentNode.visibleSubtreeCount = newVisibleSubtreeCount;
  };

  for (let nodeId = nodes.length - 1; nodeId >= 1; nodeId--) {
    walkDirectory(nodeId);
  }

  // Root is at id 0; the walk above skipped it so its visibleSubtreeCount
  // does not pick up the wrong "not-root" arithmetic. Process root now.

  const rootNode = nodes[rootId];
  const rootIndex = directories.get(rootId);
  if (rootNode == null || rootIndex == null) {
    return;
  }
  const rootChildIds = rootIndex.childIds;
  let rootTotalChildSubtreeNodeCount = 0;
  let rootTotalChildVisibleSubtreeCount = 0;
  for (let ci = 0; ci < rootChildIds.length; ci++) {
    const childId = rootChildIds[ci];
    if (childId == null) {
      continue;
    }
    const childNode = nodes[childId];
    rootTotalChildSubtreeNodeCount += childNode.subtreeNodeCount;
    rootTotalChildVisibleSubtreeCount += childNode.visibleSubtreeCount;
  }
  rootIndex.totalChildSubtreeNodeCount = rootTotalChildSubtreeNodeCount;
  rootIndex.totalChildVisibleSubtreeCount = rootTotalChildVisibleSubtreeCount;
  rebuildVisibleChildChunks(nodes, rootIndex);
  rootNode.subtreeNodeCount = 1 + rootTotalChildSubtreeNodeCount;
  rootNode.visibleSubtreeCount = rootTotalChildVisibleSubtreeCount;
}

function canInitializeOpenVisibleCounts(options: PathStoreConstructorOptions): boolean {
  return (
    options.initialExpansion === "open" &&
    (options.initialExpandedPaths == null || options.initialExpandedPaths.length === 0)
  );
}

export class PathStore {
  readonly #state: PathStoreState;

  public constructor(options: PathStoreConstructorOptions = {}) {
    const builder = new PathStoreBuilder(options);
    builder.appendPreparedPaths(preparePathEntries(options.paths ?? [], options));

    // Either initializeOpenVisibleCounts or recomputeCountsRecursive runs
    // below, and both populate subtreeNodeCount + visibleSubtreeCount from
    // each directory's childIds. Skip the builder's own backward accumulation
    // pass to avoid doing the same work twice on large ingests.
    const snapshot = builder.finish({ skipSubtreeCountPass: true });
    this.#state = createPathStoreState(snapshot, options.initialExpansion ?? "closed");
    const expandedDirectoryCount = this.initializeExpandedPaths(options.initialExpandedPaths);
    const canUseOpenVisibleCounts =
      canInitializeOpenVisibleCounts(options) ||
      ((options.initialExpansion ?? "closed") === "closed" &&
        expandedDirectoryCount === this.#state.snapshot.directories.size - 1) ||
      ((options.initialExpandedPaths?.length ?? 0) > 0 && this.hasAllDirectoriesExpanded());
    if (canUseOpenVisibleCounts) {
      initializeOpenVisibleCounts(this.#state);
    } else {
      recomputeCountsRecursive(this.#state, this.#state.snapshot.rootId);
    }
  }

  public list(path?: string): string[] {
    return listPaths(this.#state, path);
  }

  public add(path: string): void {
    const previousVisibleCount = getVisibleCount(this.#state);
    recordEvent(
      this.#state,
      finalizeEvent(this.#state, previousVisibleCount, addPath(this.#state, path)),
    );
  }

  public addPaths(paths: readonly string[]): void {
    batchEvents(this.#state, () => {
      for (const path of paths) {
        this.add(path);
      }
    });
  }

  public getVisibleCount(): number {
    return getVisibleCount(this.#state);
  }

  public getVisibleSlice(start: number, end: number): readonly PathStoreVisibleRow[] {
    return getVisibleSlice(this.#state, start, end);
  }

  public getVisibleRowContext(index: number): PathStoreVisibleRowContext | null {
    return getVisibleRowContext(this.#state, index);
  }

  public getVisibleTreeProjectionData(maxRows?: number): PathStoreVisibleTreeProjectionData {
    return getVisibleTreeProjectionDataFromState(this.#state, maxRows);
  }

  /**
   * Resolves a path to its visible row index without building a full projection
   * index. Returns null when the path is unknown or currently hidden.
   */
  public getVisibleIndex(path: string): number | null {
    return getVisibleIndexByPath(this.#state, path);
  }

  /**
   * Resolves a lookup path to the store's canonical path and item kind.
   * Lets tree adapters answer path-first queries without building a second
   * whole-tree metadata index alongside the store.
   */
  public getPathInfo(path: string): PathStorePathInfo | null {
    const nodeId = findNodeId(this.#state, path);
    if (nodeId == null) {
      return null;
    }

    const node = requireNode(this.#state, nodeId);
    return {
      depth: getNodeDepth(node),
      kind: isDirectoryNode(node) ? "directory" : "file",
      path: materializeNodePath(this.#state, nodeId),
    } satisfies PathStorePathInfo;
  }

  public isExpanded(path: string): boolean {
    const directoryNodeId = this.requireDirectoryNodeId(path);
    const directoryNode = requireNode(this.#state, directoryNodeId);
    return isDirectoryExpanded(this.#state, directoryNodeId, directoryNode);
  }

  public expand(path: string): void {
    const previousVisibleCount = getVisibleCount(this.#state);
    const event = expandPath(this.#state, path);
    if (event != null) {
      recordEvent(this.#state, finalizeEvent(this.#state, previousVisibleCount, event));
    }
  }

  public collapse(path: string): void {
    const previousVisibleCount = getVisibleCount(this.#state);
    const event = collapsePath(this.#state, path);
    if (event != null) {
      recordEvent(this.#state, finalizeEvent(this.#state, previousVisibleCount, event));
    }
  }

  public on<TType extends PathStoreEventType | "*">(
    type: TType,
    handler: (event: PathStoreEventForType<TType>) => void,
  ): () => void {
    return subscribe(this.#state, type, handler);
  }

  private initializeExpandedPaths(expandedPaths: readonly string[] | undefined): number {
    if (expandedPaths == null || expandedPaths.length === 0) {
      return 0;
    }

    let expandedDirectoryCount = 0;
    const previousChildOffsets: number[] = [];
    const previousNodeIds: number[] = [];
    let previousEndIndex = 0;
    let previousPath: string | null = null;
    const segmentTable = this.#state.snapshot.segmentTable;
    const segmentValues = segmentTable.valueById;
    const nodes = this.#state.snapshot.nodes;

    for (const path of expandedPaths) {
      if (previousPath != null && path < previousPath) {
        previousPath = null;
        previousEndIndex = 0;
        previousChildOffsets.length = 0;
        previousNodeIds.length = 0;
      }

      const hasTrailingSlash = path.length > 0 && path.charCodeAt(path.length - 1) === 47;
      const endIndex = hasTrailingSlash ? path.length - 1 : path.length;
      if (endIndex === 0) {
        previousPath = path;
        previousEndIndex = endIndex;
        previousChildOffsets.length = 0;
        previousNodeIds.length = 0;
        continue;
      }

      let sharedDepth = 0;
      let unsharedSegmentStart = 0;
      if (previousPath != null) {
        const compareLength = Math.min(endIndex, previousEndIndex);
        let prefixMatched = true;
        for (let charIndex = 0; charIndex < compareLength; charIndex += 1) {
          const charCode = path.charCodeAt(charIndex);
          if (charCode !== previousPath.charCodeAt(charIndex)) {
            prefixMatched = false;
            break;
          }
          if (charCode === 47) {
            sharedDepth += 1;
            unsharedSegmentStart = charIndex + 1;
          }
        }

        if (prefixMatched) {
          if (
            compareLength === previousEndIndex &&
            endIndex > compareLength &&
            path.charCodeAt(compareLength) === 47
          ) {
            sharedDepth += 1;
            unsharedSegmentStart = compareLength + 1;
          } else if (
            compareLength === endIndex &&
            previousEndIndex > compareLength &&
            previousPath.charCodeAt(compareLength) === 47
          ) {
            sharedDepth += 1;
            unsharedSegmentStart = endIndex + 1;
          }
        }

        sharedDepth = Math.min(sharedDepth, previousNodeIds.length);
      }

      let currentDirectoryId =
        sharedDepth === 0
          ? this.#state.snapshot.rootId
          : (previousNodeIds[sharedDepth - 1] ?? this.#state.snapshot.rootId);
      let resolvedDepth = sharedDepth;
      let foundDirectory = true;
      let segmentStart = unsharedSegmentStart;

      while (segmentStart <= endIndex) {
        const slashIndex = path.indexOf("/", segmentStart);
        const segmentEnd = slashIndex === -1 || slashIndex > endIndex ? endIndex : slashIndex;
        const segment = path.slice(segmentStart, segmentEnd);
        const currentIndex = getDirectoryIndex(this.#state, currentDirectoryId);
        const childIds = currentIndex.childIds;
        const searchStartIndex =
          resolvedDepth === sharedDepth ? (previousChildOffsets[resolvedDepth] ?? 0) : 0;
        let nextChildOffset = searchStartIndex;
        let nextNodeId: number | undefined;
        // Siblings follow the tree's comparator, which may not be name order, so no early stop.
        const searchForSegment = (startIndex: number, stopIndex: number): boolean => {
          for (nextChildOffset = startIndex; nextChildOffset < stopIndex; nextChildOffset += 1) {
            const candidateNodeId = childIds[nextChildOffset];
            if (segmentValues[nodes[candidateNodeId].nameId] === segment) {
              nextNodeId = candidateNodeId;
              return true;
            }
          }
          return false;
        };

        const foundFromStart = searchForSegment(searchStartIndex, childIds.length);
        if (!foundFromStart && searchStartIndex > 0) {
          searchForSegment(0, searchStartIndex);
        }
        if (nextNodeId === undefined) {
          foundDirectory = false;
          break;
        }

        const nextNode = requireNode(this.#state, nextNodeId);
        if (!isDirectoryNode(nextNode)) {
          foundDirectory = false;
          break;
        }

        previousChildOffsets[resolvedDepth] = nextChildOffset;
        previousNodeIds[resolvedDepth] = nextNodeId;
        currentDirectoryId = nextNodeId;
        resolvedDepth += 1;
        if (segmentEnd === endIndex) {
          break;
        }
        segmentStart = segmentEnd + 1;
      }

      previousPath = path;
      previousEndIndex = endIndex;
      previousChildOffsets.length = resolvedDepth;
      previousNodeIds.length = resolvedDepth;
      if (!foundDirectory) {
        // A missing or non-directory lookup path must not leave behind a shared
        // prefix cache for the next entry. Otherwise the next valid path can
        // inherit an ancestor depth that was never actually expanded.
        previousPath = null;
        previousEndIndex = 0;
        previousChildOffsets.length = 0;
        previousNodeIds.length = 0;
        continue;
      }

      for (let depthIndex = sharedDepth; depthIndex < resolvedDepth; depthIndex += 1) {
        const directoryNodeId = previousNodeIds[depthIndex];
        if (directoryNodeId == null) {
          continue;
        }

        const directoryNode = requireNode(this.#state, directoryNodeId);
        if (isDirectoryExpanded(this.#state, directoryNodeId, directoryNode)) {
          continue;
        }

        setDirectoryExpanded(this.#state, directoryNodeId, true, directoryNode);
        expandedDirectoryCount += 1;
      }
    }

    return expandedDirectoryCount;
  }

  private hasAllDirectoriesExpanded(): boolean {
    for (const directoryNodeId of this.#state.snapshot.directories.keys()) {
      if (directoryNodeId === this.#state.snapshot.rootId) {
        continue;
      }

      const directoryNode = requireNode(this.#state, directoryNodeId);
      if (!isDirectoryExpanded(this.#state, directoryNodeId, directoryNode)) {
        return false;
      }
    }

    return true;
  }

  private requireDirectoryNodeId(path: string): number {
    const directoryNodeId = findNodeId(this.#state, path);
    if (directoryNodeId == null) {
      throw new Error(`Path does not exist: "${path}"`);
    }

    const directoryNode = requireNode(this.#state, directoryNodeId);
    if (!isDirectoryNode(directoryNode)) {
      throw new Error(`Path is not a directory: "${path}"`);
    }

    return directoryNodeId;
  }
}
