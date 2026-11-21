// How the Files tree orders siblings: folders first, then the panel's sort
// (name, last edited, size or type) through the comparator the Changed list
// uses, so both views agree. Folders carry an mtime but never a size.

import type { FileTreeSortComparator } from "@/components/file-tree/vendored";
import { type ChangedSort, compareChangedFiles } from "../FlatFileList";

/** What ordering needs to know about one entry beyond its name. */
export interface TreeEntryMeta {
  bytes: number | null;
  modified_at: number | null;
}

const trimFolder = (path: string) => (path.endsWith("/") ? path.slice(0, -1) : path);

/** The tree's comparator for `sort`, reading each entry's metadata from `meta` by path. */
export function treeComparator(
  sort: ChangedSort,
  meta: ReadonlyMap<string, TreeEntryMeta>,
): FileTreeSortComparator {
  const compare = compareChangedFiles(sort);
  return (left, right) => {
    if (left.isDirectory !== right.isDirectory) return left.isDirectory ? -1 : 1;
    const leftPath = trimFolder(left.path);
    const rightPath = trimFolder(right.path);
    const leftMeta = meta.get(leftPath);
    const rightMeta = meta.get(rightPath);
    return compare(
      {
        name: left.basename,
        path: leftPath,
        bytes: left.isDirectory ? null : (leftMeta?.bytes ?? null),
        modified_at: leftMeta?.modified_at ?? null,
      },
      {
        name: right.basename,
        path: rightPath,
        bytes: right.isDirectory ? null : (rightMeta?.bytes ?? null),
        modified_at: rightMeta?.modified_at ?? null,
      },
    );
  };
}

/**
 * Everything the order under `sort` depends on besides the paths themselves.
 * When it changes, the tree has to re-sort; when it doesn't (sizes changing
 * under a name sort, say), the order can't have moved.
 */
export function treeOrderSignature(
  sort: ChangedSort,
  meta: ReadonlyMap<string, TreeEntryMeta>,
): string {
  if (sort !== "recent" && sort !== "size") return sort;
  const field = sort === "recent" ? "modified_at" : "bytes";
  const parts = [...meta.entries()]
    .map(([path, entry]) => `${path}\u0000${entry[field] ?? ""}`)
    .sort();
  return `${sort}\u0001${parts.join("\u0001")}`;
}
