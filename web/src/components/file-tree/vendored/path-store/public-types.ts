export interface PathStoreCompareEntry {
  basename: string;
  depth: number;
  isDirectory: boolean;
  path: string;
  segments: readonly string[];
}

export type PathStorePathComparator = (
  left: PathStoreCompareEntry,
  right: PathStoreCompareEntry,
) => number;

export interface PathStoreOptions {
  flattenEmptyDirectories?: boolean;
  sort?: "default" | PathStorePathComparator;
}

export type PathStoreInitialExpansion = "closed" | "open" | number;

export interface PathStoreConstructorOptions extends PathStoreOptions {
  initialExpansion?: PathStoreInitialExpansion;
  initialExpandedPaths?: readonly string[];
  paths?: readonly string[];
}

interface PathStoreFlattenedRowSegment {
  isTerminal: boolean;
  name: string;
  nodeId: number;
  path: string;
}

export interface PathStorePathInfo {
  depth: number;
  kind: "directory" | "file";
  path: string;
}

export interface PathStoreVisibleRow {
  depth: number;
  flattenedSegments?: readonly PathStoreFlattenedRowSegment[];
  hasChildren: boolean;
  id: number;
  isExpanded: boolean;
  isFlattened: boolean;
  kind: "directory" | "file";
  name: string;
  path: string;
}

export interface PathStoreVisibleAncestorRow {
  ancestorPaths: readonly string[];
  index: number;
  posInSet: number;
  row: PathStoreVisibleRow;
  setSize: number;
  subtreeEndIndex: number;
}

export interface PathStoreVisibleRowContext {
  ancestorPaths: readonly string[];
  ancestorRows: readonly PathStoreVisibleAncestorRow[];
  index: number;
  posInSet: number;
  row: PathStoreVisibleRow;
  setSize: number;
  subtreeEndIndex: number;
}

export interface PathStoreVisibleTreeProjectionData {
  getParentIndex: (index: number) => number;
  paths: readonly string[];
  posInSetByIndex: Int32Array<ArrayBufferLike>;
  setSizeByIndex: Int32Array<ArrayBufferLike>;
  visibleIndexByPath: Map<string, number>;
}

interface PathStoreEventInvalidation {
  affectedAncestorIds: readonly number[];
  affectedNodeIds: readonly number[];
  canonicalChanged: boolean;
  projectionChanged: boolean;
  visibleCountDelta: number | null;
}

export interface PathStoreAddEvent extends PathStoreEventInvalidation {
  operation: "add";
  path: string;
}

export interface PathStoreExpandEvent extends PathStoreEventInvalidation {
  operation: "expand";
  path: string;
}

export interface PathStoreCollapseEvent extends PathStoreEventInvalidation {
  operation: "collapse";
  path: string;
}

export type PathStoreSemanticEvent =
  PathStoreAddEvent | PathStoreExpandEvent | PathStoreCollapseEvent;

export interface PathStoreBatchEvent extends PathStoreEventInvalidation {
  events: readonly PathStoreSemanticEvent[];
  operation: "batch";
}

export type PathStoreEvent = PathStoreSemanticEvent | PathStoreBatchEvent;

export type PathStoreEventType =
  PathStoreSemanticEvent["operation"] | PathStoreBatchEvent["operation"];

export type PathStoreEventForType<TType extends PathStoreEventType | "*"> = TType extends "*"
  ? PathStoreEvent
  : Extract<PathStoreEvent, { operation: TType }>;
