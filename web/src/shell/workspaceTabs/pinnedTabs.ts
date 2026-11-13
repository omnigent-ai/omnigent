// Pinned tabs sit at the strip's start, before every other tab, as in a browser. The store
// keeps that order on every change, so a move, drag, open or reopen can't cross the line.

import { isUnknownTab, type WorkspaceTabRecord } from "./workspaceTab";

export function isPinnedTab(tab: WorkspaceTabRecord): boolean {
  return !isUnknownTab(tab) && tab.pinned === true;
}

/** `tabs` with the pinned ones first, each group in its own order; `tabs` itself if already so. */
export function withPinnedFirst<Tab extends WorkspaceTabRecord>(tabs: Tab[]): Tab[] {
  const firstUnpinned = tabs.findIndex((tab) => !isPinnedTab(tab));
  if (firstUnpinned === -1 || !tabs.slice(firstUnpinned).some(isPinnedTab)) return tabs;
  return [...tabs.filter(isPinnedTab), ...tabs.filter((tab) => !isPinnedTab(tab))];
}
