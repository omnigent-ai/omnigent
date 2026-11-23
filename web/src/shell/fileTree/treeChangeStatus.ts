// The Files tree's change marks: each changed file's own status, and for each
// folder the strongest status of anything inside it (created, then modified,
// then deleted), in the shape the file tree draws.

import type { GitStatusEntry } from "@/components/file-tree/vendored";
import type { WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";

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
