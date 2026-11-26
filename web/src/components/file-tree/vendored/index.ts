export {
  FILE_TREE_ITEM_HEIGHT,
  FILE_TREE_ROOT_SELECTOR,
  FileTree,
  type FileTreeProps,
  useFileTree,
  useFileTreeSpriteSheet,
} from "./FileTree";
export { type FileTreeResolvedIcon, resolveFileIcon } from "./icons";
export { splitNameForTruncation } from "./MiddleTruncate";
export { FileTreeModel } from "./model";
export { orderTreePaths } from "./order";
export { compareTreeEntries } from "./path-store/sort";
export type {
  FileTreeBatchOperation,
  FileTreeCompareEntry,
  FileTreeDirectoryHandle,
  FileTreeFileHandle,
  FileTreeItemHandle,
  FileTreeOptions,
  FileTreeRowDecoration,
  FileTreeScrollToPathOptions,
  FileTreeSortComparator,
  FileTreeVisibleRow,
  GitStatus,
  GitStatusEntry,
} from "./types";
