import { PathStore } from "./path-store";
import type { FileTreeSortComparator } from "./types";

/**
 * `paths` in the order a tree built from them lists them: depth first, folders
 * before files at each level by default. Folders given explicitly end with `/`.
 */
export function orderTreePaths(
  paths: readonly string[],
  options: { flattenEmptyDirectories?: boolean; sort?: FileTreeSortComparator } = {},
): string[] {
  return new PathStore({ ...options, paths }).list();
}
