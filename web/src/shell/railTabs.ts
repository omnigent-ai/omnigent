/**
 * Shared geometry + types for the right "Workspace" rail tabs.
 *
 * Extracted so the rail itself (`WorkspacePanel`) and the mobile
 * session-menu FAB (`ChatHeader`) — which mirror the same tab set —
 * can share one source of truth without importing back through
 * `AppShell` (which would be a circular import, since AppShell imports
 * both of those components).
 */

/** The selectable tabs in the right workspace rail, in display order. */
export type RightRailTab = "files" | "canvas" | "subagents" | "terminals" | "todos" | "schedules";

/**
 * Count/status badge geometry shared across the rail tabs and the mobile
 * menu. A fixed height with min-width == height (flex-centred) keeps a
 * single digit a true circle under rounded-full; longer content ("1/2",
 * double digits) grows into a pill rather than clipping. Padding-only
 * sizing can't do this — the width tracks the content, so even one digit
 * renders as a slightly-wider-than-tall oval.
 */
export const TAB_BADGE_BASE =
  "inline-flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[9px] leading-none tabular-nums";
