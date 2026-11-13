/** Shared row geometry for the home and Settings sidebars. */
export const SIDEBAR_ROW =
  "sidebar-row sidebar-compact-text h-auto min-h-0 gap-2 rounded-[var(--radius-otto-button)] px-2 py-1.5 md:py-1";

// Match the Settings sidebar's ghost-button hover treatment across every home
// sidebar row.
export const SIDEBAR_HOVER_HIGHLIGHT =
  "hover:bg-muted hover:text-foreground dark:hover:bg-muted/50";
// Keep a project-folder row highlighted while its actions menu is open (kebab
// or header context menu, both flagged `data-state=open` inside `group/header`).
// A right-click opens the context menu in a portal, dropping `:hover` from the
// header, so the hover highlight alone would leave the row un-highlighted.
export const SIDEBAR_OPEN_MENU_HIGHLIGHT =
  "group-has-[[data-state=open]]/header:bg-muted group-has-[[data-state=open]]/header:text-foreground dark:group-has-[[data-state=open]]/header:bg-muted/50";
// Active highlight also wins on hover so active items don't lose their
// background and flash when the mouse enters them.
export const SIDEBAR_ACTIVE_HIGHLIGHT =
  "bg-[var(--sidebar-active)] text-[var(--sidebar-active-foreground)] hover:bg-[var(--sidebar-active)] hover:text-[var(--sidebar-active-foreground)] dark:hover:bg-[var(--sidebar-active)] dark:hover:text-[var(--sidebar-active-foreground)]";
// Sessions shown in the displayed grid get half the active highlight.
export const SIDEBAR_GRID_MEMBER_HIGHLIGHT =
  "bg-[var(--sidebar-active)]/50 hover:bg-[var(--sidebar-active)]/50 dark:hover:bg-[var(--sidebar-active)]/50";
// The focused grid pane's row adds an inset ring on top.
export const SIDEBAR_GRID_SELECTED_RING = "ring-1 ring-inset ring-brand-accent/50";
