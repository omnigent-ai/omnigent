// App-global preference for whether the right workspace rail starts open for
// sessions that do not yet have per-session rail state.

const STORAGE_KEY = "omnigent:right-workspace-default-open";

export const RIGHT_WORKSPACE_DEFAULT_OPEN = true;

export type RightWorkspaceDefaultVisibility = "show" | "hide";

export function visibilityToOpen(value: RightWorkspaceDefaultVisibility): boolean {
  return value === "show";
}

export function openToVisibility(open: boolean): RightWorkspaceDefaultVisibility {
  return open ? "show" : "hide";
}

/** Read whether brand-new sessions should show the right workspace rail. */
export function readRightWorkspaceDefaultOpen(): boolean {
  if (typeof window === "undefined") return RIGHT_WORKSPACE_DEFAULT_OPEN;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === "show") return true;
    if (raw === "hide") return false;
    return RIGHT_WORKSPACE_DEFAULT_OPEN;
  } catch {
    return RIGHT_WORKSPACE_DEFAULT_OPEN;
  }
}

/** Persist whether brand-new sessions should show the right workspace rail. */
export function writeRightWorkspaceDefaultOpen(open: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, openToVisibility(open));
  } catch {
    // localStorage quota/access errors should not break settings.
  }
}
