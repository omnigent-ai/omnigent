import type { PathStorePathComparator } from "./public-types";

export type NodeId = number;
export type SegmentId = number;

export const PATH_STORE_NODE_KIND_FILE = 0;
export const PATH_STORE_NODE_KIND_DIRECTORY = 1;

type PathStoreNodeKind = typeof PATH_STORE_NODE_KIND_FILE | typeof PATH_STORE_NODE_KIND_DIRECTORY;

export const PATH_STORE_NODE_FLAG_EXPLICIT: number = 1 << 0;
export const PATH_STORE_NODE_FLAG_ROOT: number = 1 << 1;

const PATH_STORE_NODE_FLAGS_MASK = PATH_STORE_NODE_FLAG_EXPLICIT | PATH_STORE_NODE_FLAG_ROOT;
const PATH_STORE_NODE_KIND_SHIFT = 3;
const PATH_STORE_NODE_KIND_MASK = 1 << PATH_STORE_NODE_KIND_SHIFT;
const PATH_STORE_NODE_DEPTH_SHIFT = 4;

export interface SegmentSortKey {
  lowerValue: string;
  tokens: readonly (number | string)[];
}

export interface SegmentTable {
  // Map outperforms plain `Object.create(null)` for the string-keyed lookups
  // this table does during bulk ingest: ~940K hits + ~50K misses on
  // linux-10x. Map<string, number> is consistent O(1) and measurably faster
  // than the dictionary-mode plain object the previous implementation
  // transitioned into after a few thousand keys.
  idByValue: Map<string, SegmentId>;
  valueById: string[];
  sortKeyById: (SegmentSortKey | undefined)[];
}

export interface PathStoreNode {
  parentId: NodeId;
  nameId: SegmentId;
  depthAndFlags: number;
  subtreeNodeCount: number;
  visibleSubtreeCount: number;
}

export function createNodeDepthAndFlags(
  depth: number,
  flags: number,
  kind: PathStoreNodeKind = PATH_STORE_NODE_KIND_FILE,
): number {
  return (depth << PATH_STORE_NODE_DEPTH_SHIFT) | (kind << PATH_STORE_NODE_KIND_SHIFT) | flags;
}

export function getNodeDepth(node: PathStoreNode): number {
  return node.depthAndFlags >>> PATH_STORE_NODE_DEPTH_SHIFT;
}

export function getNodeKind(node: PathStoreNode): PathStoreNodeKind {
  return ((node.depthAndFlags & PATH_STORE_NODE_KIND_MASK) >>
    PATH_STORE_NODE_KIND_SHIFT) as PathStoreNodeKind;
}

export function isDirectoryNode(node: PathStoreNode): boolean {
  return (node.depthAndFlags & PATH_STORE_NODE_KIND_MASK) !== 0;
}

function getNodeFlags(node: PathStoreNode): number {
  return node.depthAndFlags & PATH_STORE_NODE_FLAGS_MASK;
}

export function hasNodeFlag(node: PathStoreNode, flag: number): boolean {
  return (getNodeFlags(node) & flag) !== 0;
}

export function addNodeFlag(node: PathStoreNode, flag: number): void {
  node.depthAndFlags |= flag;
}

export interface DirectoryChildIndex {
  childIds: NodeId[];
  childIdByNameId: Map<SegmentId, NodeId> | null;
  childPositionById: Map<NodeId, number> | null;
  childVisibleChunkSums: Int32Array<ArrayBufferLike> | null;
  totalChildSubtreeNodeCount: number;
  totalChildVisibleSubtreeCount: number;
}

export interface ResolvedPathStoreOptions {
  flattenEmptyDirectories: boolean;
  sort: "default" | PathStorePathComparator;
}

export interface PreparedPath {
  basename: string;
  isDirectory: boolean;
  path: string;
  segments: readonly string[];
}

export interface LookupPath {
  requiresDirectory: boolean;
  segments: readonly string[];
}

export interface PathStoreSnapshot {
  directories: Map<NodeId, DirectoryChildIndex>;
  nodes: PathStoreNode[];
  options: ResolvedPathStoreOptions;
  rootId: NodeId;
  segmentTable: SegmentTable;
}
