export type GitStatus = "added" | "deleted" | "ignored" | "modified" | "renamed" | "untracked";

export interface GitStatusEntry {
  path: string;
  status: GitStatus;
}

type FileTreeInitialExpansion = "closed" | "open" | number;

interface FileTreeControllerOptions {
  paths: readonly string[];
  flattenEmptyDirectories?: boolean;
  initialExpansion?: FileTreeInitialExpansion;
  initialExpandedPaths?: readonly string[];
  initialSearchQuery?: string | null;
  initialSelectedPaths?: readonly string[];
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
  visibleIndex: number;
}
