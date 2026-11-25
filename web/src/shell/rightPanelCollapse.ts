// Which control collapses the right panel: the floating tabs sidebar's own corner button, or the
// session header's toggle.

export interface RightPanelCollapseState {
  /** The panes float now, at this window's width. */
  floatingPanesShown: boolean;
  /** The workspace sidebar shows as tabs (workspace_sidebar_v2). */
  tabsSidebar: boolean;
  /** The tabs sidebar sits in a narrow window's drawer, which closes itself. */
  tabsInDrawer: boolean;
  /** The right panel is meant to be open. */
  rightPanelOpen: boolean;
  /** The width the open sidebar gets, after the window and left sidebar take theirs. */
  railWidth: number;
  /** The width the sidebar's strip needs to show its Collapse button; null until it has laid out. */
  controlsWidth: number | null;
}

/** Whether the open sidebar collapses from its own button, so the header shows its toggle only to open it. */
export function rightPanelCollapsesItself(state: RightPanelCollapseState): boolean {
  if (!state.floatingPanesShown || !state.tabsSidebar || state.tabsInDrawer) return false;
  // An open sidebar too narrow to show its own button leaves the header its toggle.
  if (!state.rightPanelOpen) return true;
  return state.controlsWidth !== null && state.railWidth >= state.controlsWidth;
}
