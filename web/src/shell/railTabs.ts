/**
 * Shared geometry + types for the right "Workspace" rail tabs. Its own module
 * so `WorkspacePanel` and the mobile FAB (`ChatHeader`) share one source of
 * truth without importing back through `AppShell` (a cycle).
 */

/**
 * The fixed-order workspace-rail tabs used for iteration/fallback. The
 * runtime list is the single source of truth for the static tab ids, so
 * consumers that validate or iterate them can't drift behind a newly added
 * tab. "sidechat" is a dynamic-tab mode (a side-chat tab is selected), not a
 * fixed tab in this list. "github" is the stable stored id for the Pull
 * Requests tab; renaming it drops saved choices.
 */
export const RIGHT_RAIL_TABS = ["files", "changes", "github", "subagents", "browser"] as const;

export type RightRailTab = (typeof RIGHT_RAIL_TABS)[number] | "sidechat";

/**
 * Count/status badge geometry. Fixed height with min-width == height keeps a
 * single digit a circle while "1/2" / double digits grow into a pill.
 */
export const TAB_BADGE_BASE =
  "inline-flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[9px] leading-none tabular-nums";
