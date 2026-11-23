import type { PathStorePathComparator } from "./path-store/public-types";

export type GitStatus = "added" | "deleted" | "ignored" | "modified" | "renamed" | "untracked";

export interface GitStatusEntry {
  path: string;
  status: GitStatus;
}

type FileTreeInitialExpansion = "closed" | "open" | number;

/** What a sort comparator sees of each sibling it orders. */
export type { PathStoreCompareEntry as FileTreeCompareEntry } from "./path-store/public-types";

/** Orders siblings; folders and files alike, so a comparator may put folders first. */
export type FileTreeSortComparator = PathStorePathComparator;

/** Text a row shows after its name; `parts` color pieces of it, `title` explains it. */
export interface FileTreeRowDecoration {
  text: string;
  title?: string;
  parts?: readonly { text: string; color: string }[];
}

/** One change for `FileTreeModel.batch`; a folder needs `recursive` to remove its contents. */
export type FileTreeBatchOperation =
  { type: "add"; path: string } | { type: "remove"; path: string; recursive?: boolean };

interface FileTreeControllerOptions {
  paths: readonly string[];
  flattenEmptyDirectories?: boolean;
  initialExpansion?: FileTreeInitialExpansion;
  initialExpandedPaths?: readonly string[];
  initialSearchQuery?: string | null;
  initialSelectedPaths?: readonly string[];
  /** How siblings are ordered; defaults to folders first, then by name. */
  sort?: FileTreeSortComparator;
}

export type FileTreeOptions = FileTreeControllerOptions & {
  gitStatus?: readonly GitStatusEntry[];
  /** Whether file icons use per-language colors. Defaults to true. */
  coloredIcons?: boolean;
  onSelectionChange?: (selectedPaths: readonly string[]) => void;
};

interface FileTreeVisibleSegment {
  isTerminal: boolean;
  name: string;
  path: string;
}

export interface FileTreeVisibleRow {
  ancestorPaths: readonly string[];
  depth: number;
  flattenedSegments?: readonly FileTreeVisibleSegment[];
  hasChildren: boolean;
  index: number;
  isFocused: boolean;
  isSelected: boolean;
  isExpanded: boolean;
  isFlattened: boolean;
  kind: "directory" | "file";
  name: string;
  path: string;
  posInSet: number;
  setSize: number;
}

interface FileTreeItemHandleBase {
  deselect: () => void;
  focus: () => void;
  getPath: () => string;
  isFocused: () => boolean;
  isSelected: () => boolean;
  select: () => void;
}

export type FileTreeDirectoryHandle = FileTreeItemHandleBase & {
  collapse: () => void;
  expand: () => void;
  isDirectory: () => true;
  isExpanded: () => boolean;
  toggle: () => void;
};

export type FileTreeFileHandle = FileTreeItemHandleBase & {
  isDirectory: () => false;
};

export type FileTreeItemHandle = FileTreeDirectoryHandle | FileTreeFileHandle;

export type FileTreeScrollOffset = "top" | "center" | "nearest";

export interface FileTreeScrollToPathOptions {
  /** Whether the target also becomes the focused row. Defaults to true. */
  focus?: boolean;
  offset?: FileTreeScrollOffset;
}

export interface FileTreeScrollRequest {
  id: number;
  offset: FileTreeScrollOffset;
  /** The row to reveal, whose index can move if the tree changes before it's shown. */
  path: string;
  visibleIndex: number;
}
