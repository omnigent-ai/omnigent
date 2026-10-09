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

/**
 * Footprint of one row action icon button — a 14px glyph plus 2px of padding
 * on each side (``size-3.5`` + ``p-0.5``).
 *
 * Rendered as an empty spacer where a row has no download button (a deleted
 * file, a directory) so the copy button beside it keeps the same x as on rows
 * that do. Without it, an absent download slides the copy button 20px right on
 * exactly those rows.
 */
export const ROW_ACTION_SIZE_CLASS = "size-[18px]";

/**
 * Width of the git-status marker column at the end of a tree row's name button
 * — the A/M/D badge on files, the dirty dot on directories.
 *
 * Both markers are centred in this same box so they share one column. Sizing
 * each to its own content instead left them ~4px apart: the dot already had a
 * 22px box while the letter was a variable-width badge centred on itself, so
 * the two markers never quite lined up down the tree.
 */
export const ROW_STATUS_SLOT_CLASS = "w-[22px]";

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

function modifiedDate(modifiedAt: number): Date {
  // Filesystem payloads use Unix seconds; tolerate milliseconds for callers
  // that already hold a JavaScript timestamp.
  return new Date(modifiedAt < 10_000_000_000 ? modifiedAt * 1000 : modifiedAt);
}

export function formatEditedRelative(modifiedAt: number, now = Date.now()): string {
  const elapsedSeconds = Math.max(0, Math.floor((now - modifiedDate(modifiedAt).getTime()) / 1000));
  if (elapsedSeconds < 60) return "just now";
  if (elapsedSeconds < 3600) return `${Math.floor(elapsedSeconds / 60)} min ago`;
  if (elapsedSeconds < 86_400) return `${Math.floor(elapsedSeconds / 3600)} hr ago`;
  const days = Math.floor(elapsedSeconds / 86_400);
  return days < 30 ? `${days}d ago` : modifiedDate(modifiedAt).toLocaleDateString();
}

export function formatEditedCompact(modifiedAt: number, now = Date.now()): string {
  const elapsedSeconds = Math.max(0, Math.floor((now - modifiedDate(modifiedAt).getTime()) / 1000));
  if (elapsedSeconds < 60) return "now";
  if (elapsedSeconds < 3600) return `${Math.floor(elapsedSeconds / 60)}m`;
  if (elapsedSeconds < 86_400) return `${Math.floor(elapsedSeconds / 3600)}h`;
  if (elapsedSeconds < 604_800) return `${Math.floor(elapsedSeconds / 86_400)}d`;
  return modifiedDate(modifiedAt).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function formatEditedAbsolute(modifiedAt: number): string {
  return modifiedDate(modifiedAt).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
