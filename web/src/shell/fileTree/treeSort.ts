// How the Files tree orders siblings: folders first, then the panel's sort
// (name, last edited, size or type) through the comparator the Changed list
// uses, so both views agree. Folders carry an mtime but never a size.

import type { FileTreeCompareEntry, FileTreeSortComparator } from "@/components/file-tree/vendored";
import { type ChangedSort, compareChangedFiles } from "../FlatFileList";

/** What ordering needs to know about one entry beyond its name. */
export interface TreeEntryMeta {
  bytes: number | null;
  modified_at: number | null;
}

/** The sibling an entry sits under at `depth`: a folder, unless it is the entry itself. */
interface Sibling {
  name: string;
  path: string;
  isDirectory: boolean;
}

function siblingAt(entry: FileTreeCompareEntry, depth: number): Sibling {
  const isEntry = depth === entry.segments.length - 1;
  return {
    name: entry.segments[depth] ?? entry.basename,
    path: entry.segments.slice(0, depth + 1).join("/"),
    isDirectory: isEntry ? entry.isDirectory : true,
  };
}

/**
 * The tree's comparator for `sort`, reading each entry's metadata from `meta` by path.
 * The tree sorts whole paths, so two paths are ordered by the siblings where they
 * first differ: a folder sorts by its own metadata, never by a file inside it.
 */
export function treeComparator(
  sort: ChangedSort,
  meta: ReadonlyMap<string, TreeEntryMeta>,
): FileTreeSortComparator {
  const compare = compareChangedFiles(sort);
  const sortable = (sibling: Sibling) => {
    const siblingMeta = meta.get(sibling.path);
    return {
      name: sibling.name,
      path: sibling.path,
      bytes: sibling.isDirectory ? null : (siblingMeta?.bytes ?? null),
      modified_at: siblingMeta?.modified_at ?? null,
    };
  };
  return (leftEntry, rightEntry) => {
    const shared = Math.min(leftEntry.segments.length, rightEntry.segments.length);
    let depth = 0;
    while (depth < shared && leftEntry.segments[depth] === rightEntry.segments[depth]) depth++;
    // One path holds the other: a folder comes before what is inside it.
    if (depth === shared) return leftEntry.segments.length - rightEntry.segments.length;
    const left = siblingAt(leftEntry, depth);
    const right = siblingAt(rightEntry, depth);
    if (left.isDirectory !== right.isDirectory) return left.isDirectory ? -1 : 1;
    return compare(sortable(left), sortable(right));
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
