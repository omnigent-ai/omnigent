import type { WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";

/**
 * Fixed width of a file row's trailing metadata column (byte size in the
 * tree, diffstat in the changed list).
 *
 * Sized rather than content-fit on purpose: ``formatBytes`` ranges from
 * ``"985 B"`` to ``"463 KB"``, and letting that set the column width drags
 * everything to its left — the copy button, the download button, the git
 * status marker — into a different x on every row (~16px of drift). A fixed
 * column makes those land in one vertical line down the panel.
 *
 * Directory rows render this slot EMPTY rather than omitting it, so the tree's
 * folders and files share the same grid. Exported so the two row components
 * cannot drift apart.
 */
export const ROW_META_SLOT_CLASS = "w-14";

export function gitStatusLetter(status: WorkspaceChangedFile["status"]): string {
  switch (status) {
    case "created":
      return "A";
    case "deleted":
      return "D";
    case "modified":
      return "M";
  }
}

export function gitStatusLabel(status: WorkspaceChangedFile["status"]): string {
  switch (status) {
    case "created":
      return "Added";
    case "deleted":
      return "Deleted";
    case "modified":
      return "Modified";
  }
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  for (let i = 0; i < units.length; i += 1) {
    if (value < 1024 || i === units.length - 1) {
      return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[i]}`;
    }
    value /= 1024;
  }
  return `${bytes} B`;
}
