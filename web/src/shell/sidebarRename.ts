/** Opens a row's inline rename; returns false when the row can't take it now. */
export type StartSidebarRename = () => boolean;

const startersBySessionId = new Map<string, Set<StartSidebarRename>>();

/** Lets a sidebar row receive the rename shortcut; returns the unregister cleanup. */
export function registerSidebarRename(sessionId: string, start: StartSidebarRename): () => void {
  let starters = startersBySessionId.get(sessionId);
  if (!starters) {
    starters = new Set();
    startersBySessionId.set(sessionId, starters);
  }
  starters.add(start);
  return () => {
    const current = startersBySessionId.get(sessionId);
    if (!current) return;
    current.delete(start);
    if (current.size === 0) startersBySessionId.delete(sessionId);
  };
}

/** Opens the session's inline rename in the sidebar; false when no shown row could. */
export function startSidebarRename(sessionId: string): boolean {
  for (const start of startersBySessionId.get(sessionId) ?? []) {
    if (start()) return true;
  }
  return false;
}

/** A row inside a closed sidebar (desktop collapsed or mobile drawer parked) is off screen. */
export function isInClosedSidebar(row: Element): boolean {
  return row.closest("[data-collapsed]") !== null;
}
