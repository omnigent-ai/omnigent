/**
 * Shared geometry + types for the right "Workspace" rail tabs. Its own module
 * so `WorkspacePanel` and the mobile FAB (`ChatHeader`) share one source of
 * truth without importing back through `AppShell` (a cycle).
 */

/**
 * The selectable tabs in the right workspace rail, in display order. The
 * runtime list is the single source of truth (the type derives from it) so
 * consumers that validate or iterate tabs — e.g. the persisted-state
 * sanitizer — can never drift behind a newly added tab.
 */
export const RIGHT_RAIL_TABS = ["files", "changes", "github", "subagents", "browser"] as const;

export type RightRailTab = (typeof RIGHT_RAIL_TABS)[number];

/**
 * Count/status badge geometry. Fixed height with min-width == height keeps a
 * single digit a circle while "1/2" / double digits grow into a pill.
 */
export const TAB_BADGE_BASE =
  "inline-flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[9px] leading-none tabular-nums";
