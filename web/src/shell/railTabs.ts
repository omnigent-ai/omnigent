/**
 * Shared geometry + types for the right "Workspace" rail tabs. Its own module
 * so `WorkspacePanel` and the mobile FAB (`ChatHeader`) share one source of
 * truth without importing back through `AppShell` (a cycle).
 */

/** The selectable tabs in the right workspace rail, in display order. */
export type RightRailTab = "files" | "subagents" | "terminals" | "todos" | "browser";

/**
 * Count/status badge geometry. Fixed height with min-width == height keeps a
 * single digit a circle while "1/2" / double digits grow into a pill.
 */
export const TAB_BADGE_BASE =
  "inline-flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[9px] leading-none tabular-nums";
