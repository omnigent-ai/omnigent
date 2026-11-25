import {
  appendChildReference,
  createDirectoryChildIndex,
  rebuildDirectoryChildAggregates,
} from "./child-index";
import {
  addNodeFlag,
  createNodeDepthAndFlags,
  getNodeDepth,
  hasNodeFlag,
  isDirectoryNode,
  PATH_STORE_NODE_FLAG_EXPLICIT,
  PATH_STORE_NODE_FLAG_ROOT,
  PATH_STORE_NODE_KIND_DIRECTORY,
} from "./internal-types";
import type {
  DirectoryChildIndex,
  NodeId,
  PathStoreNode,
  PathStoreSnapshot,
  PreparedPath,
  ResolvedPathStoreOptions,
  SegmentSortKey,
} from "./internal-types";

// Options passed to PathStoreBuilder.finish(). The PathStore constructor always
// runs its own per-node count computation afterwards, so it sets
// skipSubtreeCountPass to avoid the builder's backward accumulation pass.
interface BuilderFinishOptions {
  skipSubtreeCountPass?: boolean;
}

import { resolvePathStoreOptions } from "./options";
import { parseInputPath } from "./path";
import type {
  PathStoreCompareEntry,
  PathStoreOptions,
  PathStorePathComparator,
} from "./public-types";
import { internSegment, createSegmentTable } from "./segments";
import { comparePreparedPaths, comparePreparedPathsWithCachedSortKeys } from "./sort";

function createCompareEntry(preparedPath: PreparedPath): PathStoreCompareEntry {
  return {
    basename: preparedPath.basename,
    depth: preparedPath.segments.length,
    isDirectory: preparedPath.isDirectory,
    path: preparedPath.path,
    segments: preparedPath.segments,
  };
}

function compareWithSortOption(
  left: PreparedPath,
  right: PreparedPath,
  sort: "default" | PathStorePathComparator,
): number {
  if (sort === "default") {
    return comparePreparedPaths(left, right);
  }

  return sort(createCompareEntry(left), createCompareEntry(right));
}

function createRootNode(): PathStoreNode {
  return {
    depthAndFlags: createNodeDepthAndFlags(
      0,
      PATH_STORE_NODE_FLAG_EXPLICIT | PATH_STORE_NODE_FLAG_ROOT,
      PATH_STORE_NODE_KIND_DIRECTORY,
    ),
    nameId: 0,
    parentId: 0,
    subtreeNodeCount: 1,
    visibleSubtreeCount: 1,
  };
}

function computeSharedPrefixLength(left: readonly string[], right: readonly string[]): number {
  const maxLength = Math.min(left.length, right.length);
  for (let index = 0; index < maxLength; index++) {
    if (left[index] !== right[index]) {
      return index;
    }
  }

  return maxLength;
}

function getDirectoryDepth(preparedPath: PreparedPath): number {
  return preparedPath.isDirectory ? preparedPath.segments.length : preparedPath.segments.length - 1;
}

export function preparePathEntries(
  paths: readonly string[],
  options: PathStoreOptions = {},
): PreparedPath[] {
  const resolvedOptions = resolvePathStoreOptions(options);
  const preparedPaths = paths.map((path) => parseInputPath(path));

  preparedPaths.sort((left, right) => compareWithSortOption(left, right, resolvedOptions.sort));

  return preparedPaths;
}

export class PathStoreBuilder {
  private readonly directories = new Map<NodeId, DirectoryChildIndex>();
  private readonly directoryStack: NodeId[] = [0];
  private lastPreparedPath: PreparedPath | null = null;
  private readonly nodes: PathStoreNode[] = [createRootNode()];
  private readonly options: ResolvedPathStoreOptions;
  private readonly segmentSortKeyCache = new Map<string, SegmentSortKey>();
  private readonly segmentTable = createSegmentTable();

  public constructor(options: PathStoreOptions = {}) {
    this.options = resolvePathStoreOptions(options);

    this.directories.set(0, createDirectoryChildIndex());
  }

  public appendPreparedPaths(preparedPaths: readonly PreparedPath[], validateOrder = true): this {
    for (const preparedPath of preparedPaths) {
      this.appendPreparedPath(preparedPath, validateOrder);
    }

    return this;
  }

  public finish(options: BuilderFinishOptions = {}): PathStoreSnapshot {
    const skipSubtreeCountPass = options.skipSubtreeCountPass === true;
    if (!skipSubtreeCountPass) {
      this.computeSubtreeCounts(0);
    }
    return {
      directories: this.directories,
      nodes: this.nodes,
      options: this.options,
      rootId: 0,
      segmentTable: this.segmentTable,
    };
  }

  private appendPreparedPath(preparedPath: PreparedPath, validateOrder: boolean): void {
    if (this.lastPreparedPath != null) {
      if (preparedPath.path === this.lastPreparedPath.path) {
        throw new Error(`Duplicate path: "${preparedPath.path}"`);
      }

      if (validateOrder) {
        const orderComparison =
          this.options.sort === "default"
            ? comparePreparedPathsWithCachedSortKeys(
                this.lastPreparedPath,
                preparedPath,
                this.segmentSortKeyCache,
              )
            : compareWithSortOption(this.lastPreparedPath, preparedPath, this.options.sort);
        if (orderComparison > 0) {
          throw new Error(
            `Builder input must be sorted before appendPaths(): "${preparedPath.path}"`,
          );
        }
      }
    }

    const previousPath = this.lastPreparedPath;
    const currentDirectoryDepth = getDirectoryDepth(preparedPath);
    const previousDirectoryDepth = previousPath == null ? 0 : getDirectoryDepth(previousPath);
    const sharedPrefixLength =
      previousPath == null
        ? 0
        : computeSharedPrefixLength(previousPath.segments, preparedPath.segments);
    const sharedDirectoryDepth = Math.min(
      sharedPrefixLength,
      currentDirectoryDepth,
      previousDirectoryDepth,
    );

    this.directoryStack.length = sharedDirectoryDepth + 1;

    for (
      let segmentIndex = sharedDirectoryDepth;
      segmentIndex < currentDirectoryDepth;
      segmentIndex++
    ) {
      const parentId = this.directoryStack[this.directoryStack.length - 1];
      if (parentId === undefined) {
        throw new Error("Directory stack underflow while building the path store");
      }

      const childId = validateOrder
        ? this.getOrCreateDirectoryChild(parentId, preparedPath.segments[segmentIndex])
        : this.createDirectoryChild(parentId, preparedPath.segments[segmentIndex]);
      this.directoryStack.push(childId);
    }

    if (preparedPath.isDirectory) {
      const directoryId = this.directoryStack[this.directoryStack.length - 1];
      if (directoryId === undefined) {
        throw new Error(`Unable to resolve directory node for "${preparedPath.path}"`);
      }

      this.promoteDirectoryToExplicit(directoryId, preparedPath.path);
      this.lastPreparedPath = preparedPath;
      return;
    }

    const parentId = this.directoryStack[this.directoryStack.length - 1];
    if (parentId === undefined) {
      throw new Error(`Unable to resolve file parent for "${preparedPath.path}"`);
    }

    if (validateOrder) {
      this.createFileChild(parentId, preparedPath.basename, preparedPath.path);
    } else {
      this.createFileChildUnchecked(parentId, preparedPath.basename);
    }
    this.lastPreparedPath = preparedPath;
  }

  private createFileChild(parentId: NodeId, basename: string, path: string): NodeId {
    const nameId = internSegment(this.segmentTable, basename);
    const parentIndex = this.getDirectoryIndex(parentId);
    const nameMap = parentIndex.childIdByNameId;
    if (nameMap != null) {
      const existingChildId = nameMap.get(nameId);
      if (existingChildId !== undefined) {
        throw new Error(`Path collides with an existing entry: "${path}"`);
      }
    }

    const parentNode = this.nodes[parentId];
    if (parentNode === undefined) {
      throw new Error(`Unknown parent node ID: ${String(parentId)}`);
    }

    const nodeId = this.nodes.length;
    this.nodes.push({
      depthAndFlags: createNodeDepthAndFlags(getNodeDepth(parentNode) + 1, 0),
      nameId,
      parentId,
      subtreeNodeCount: 1,
      visibleSubtreeCount: 1,
    });

    if (nameMap != null) {
      nameMap.set(nameId, nodeId);
    }
    appendChildReference(parentIndex, nodeId);
    return nodeId;
  }

  // Bulk-ingested file nodes leave full paths lazy so first render only pays to
  // materialize the tiny visible window instead of caching every file string up
  // front.
  private createFileChildUnchecked(parentId: NodeId, basename: string): NodeId {
    const nameId = internSegment(this.segmentTable, basename);
    const parentIndex = this.getDirectoryIndex(parentId);
    const parentNode = this.nodes[parentId];
    if (parentNode === undefined) {
      throw new Error(`Unknown parent node ID: ${String(parentId)}`);
    }

    const nodeId = this.nodes.length;
    this.nodes.push({
      depthAndFlags: createNodeDepthAndFlags(getNodeDepth(parentNode) + 1, 0),
      nameId,
      parentId,
      subtreeNodeCount: 1,
      visibleSubtreeCount: 1,
    });

    if (parentIndex.childIdByNameId != null) {
      parentIndex.childIdByNameId.set(nameId, nodeId);
    }
    appendChildReference(parentIndex, nodeId);
    return nodeId;
  }

  private getOrCreateDirectoryChild(parentId: NodeId, segment: string): NodeId {
    const nameId = internSegment(this.segmentTable, segment);
    const parentIndex = this.getDirectoryIndex(parentId);
    if (parentIndex.childIdByNameId != null) {
      const existingChildId = parentIndex.childIdByNameId.get(nameId);
      if (existingChildId !== undefined) {
        const existingNode = this.nodes[existingChildId];
        if (existingNode != null && !isDirectoryNode(existingNode)) {
          throw new Error(
            `Path collides with an existing file while creating directory "${segment}"`,
          );
        }

        return existingChildId;
      }
    }

    const parentNode = this.nodes[parentId];
    if (parentNode === undefined) {
      throw new Error(`Unknown parent node ID: ${String(parentId)}`);
    }

    const nodeId = this.nodes.length;
    this.nodes.push({
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

    if (parentIndex.childIdByNameId != null) {
      parentIndex.childIdByNameId.set(nameId, nodeId);
    }
    appendChildReference(parentIndex, nodeId);
    this.directories.set(nodeId, createDirectoryChildIndex());
    return nodeId;
  }

  // Sorted unique prepared input only introduces brand-new directories beyond
  // the shared prefix with the previous path, so no existing-child lookup is
  // required in this fast path.
  private createDirectoryChild(parentId: NodeId, segment: string): NodeId {
    const nameId = internSegment(this.segmentTable, segment);
    const parentIndex = this.getDirectoryIndex(parentId);
    const parentNode = this.nodes[parentId];
    if (parentNode === undefined) {
      throw new Error(`Unknown parent node ID: ${String(parentId)}`);
    }

    const nodeId = this.nodes.length;
    this.nodes.push({
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

    if (parentIndex.childIdByNameId != null) {
      parentIndex.childIdByNameId.set(nameId, nodeId);
    }
    appendChildReference(parentIndex, nodeId);
    this.directories.set(nodeId, createDirectoryChildIndex());
    return nodeId;
  }

  private promoteDirectoryToExplicit(directoryId: NodeId, path: string): void {
    const directoryNode = this.nodes[directoryId];
    if (directoryNode === undefined) {
      throw new Error(`Unknown directory node ID: ${String(directoryId)}`);
    }

    if (!isDirectoryNode(directoryNode)) {
      throw new Error(`Path is not a directory: "${path}"`);
    }

    if (hasNodeFlag(directoryNode, PATH_STORE_NODE_FLAG_EXPLICIT)) {
      throw new Error(`Duplicate path: "${path}"`);
    }

    addNodeFlag(directoryNode, PATH_STORE_NODE_FLAG_EXPLICIT);
  }

  private getDirectoryIndex(directoryId: NodeId): DirectoryChildIndex {
    const existingIndex = this.directories.get(directoryId);
    if (existingIndex !== undefined) {
      return existingIndex;
    }

    throw new Error(`Unknown directory child index for node ${String(directoryId)}`);
  }

  // Computes subtree counts after bulk ingest so projection math can read
  // aggregate counts without changing the canonical storage layout.
  private computeSubtreeCounts(nodeId: NodeId): number {
    const node = this.nodes[nodeId];
    if (node === undefined) {
      throw new Error(`Unknown node ID: ${String(nodeId)}`);
    }

    if (!isDirectoryNode(node)) {
      node.subtreeNodeCount = 1;
      node.visibleSubtreeCount = 1;
      return 1;
    }

    const directoryIndex = this.getDirectoryIndex(nodeId);
    let subtreeNodeCount = 1;
    for (const childId of directoryIndex.childIds) {
      subtreeNodeCount += this.computeSubtreeCounts(childId);
    }

    // Children already have final counts from the recursive descent above, so
    // the directory can derive its cached child aggregates before writing its
    // own subtree totals.
    rebuildDirectoryChildAggregates(this.nodes, directoryIndex);
    node.subtreeNodeCount = subtreeNodeCount;
    node.visibleSubtreeCount = subtreeNodeCount;
    return subtreeNodeCount;
  }
}
