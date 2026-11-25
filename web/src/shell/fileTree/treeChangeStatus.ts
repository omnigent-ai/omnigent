// The Files tree's marks: each change, each folder's strongest change inside it,
// and the listed entries Git ignores, which the tree dims with their contents.

import type { GitStatusEntry } from "@/components/file-tree/vendored";
import type { WorkspaceChangedFile, WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

type ChangeStatus = WorkspaceChangedFile["status"];

const PRIORITY: Record<ChangeStatus, number> = { created: 3, modified: 2, deleted: 1 };
const TREE_STATUS: Record<ChangeStatus, GitStatusEntry["status"]> = {
  created: "added",
  modified: "modified",
  deleted: "deleted",
};

/**
 * Status entries for changed files, given relative to the tree's root, plus one
 * for every folder above them. Folders are tree paths, ending in "/".
 */
export function treeChangeStatuses(
  changed: readonly { path: string; status: ChangeStatus }[],
): GitStatusEntry[] {
  const folders = new Map<string, ChangeStatus>();
  const entries: GitStatusEntry[] = [];
  for (const file of changed) {
    entries.push({ path: file.path, status: TREE_STATUS[file.status] });
    const parts = file.path.split("/");
    for (let depth = 1; depth < parts.length; depth++) {
      const folder = `${parts.slice(0, depth).join("/")}/`;
      const current = folders.get(folder);
      if (!current || PRIORITY[file.status] > PRIORITY[current]) folders.set(folder, file.status);
    }
  }
  for (const [path, status] of folders) entries.push({ path, status: TREE_STATUS[status] });
  return entries;
}

/** Status entries for the listed entries Git ignores. Folders are tree paths, ending in "/". */
export function treeIgnoredStatuses(entries: Iterable<WorkspaceFile>): GitStatusEntry[] {
  const statuses: GitStatusEntry[] = [];
  for (const entry of entries) {
    if (!entry.ignored) continue;
    statuses.push({
      path: entry.type === "directory" ? `${entry.path}/` : entry.path,
      status: "ignored",
    });
  }
  return statuses;
}
