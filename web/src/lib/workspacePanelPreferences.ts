// Persisted, app-global preference for whether a brand-new chat's right
// Workspace rail (Files / Agents / Shells) starts open or collapsed.
//
// This only seeds sessions that have no saved per-chat `open` state. Once a
// user toggles the rail in a session, that session's own
// `SessionWorkspaceState.open` wins on restore. Set from Appearance settings.

const STORAGE_KEY = "omnigent:default-workspace-panel";

export const workspacePanelDefaults = ["open", "collapsed"] as const;
export type WorkspacePanelDefault = (typeof workspacePanelDefaults)[number];

/** Match today's product default: new chats open the Workspace rail. */
export const WORKSPACE_PANEL_DEFAULT: WorkspacePanelDefault = "open";

/** Return whether a string is one of the selectable Workspace panel defaults. */
export function isWorkspacePanelDefault(
  value: string | null | undefined,
): value is WorkspacePanelDefault {
  return value === "open" || value === "collapsed";
}

/**
 * Normalize a stored Workspace panel default to the product default.
 *
 * Unknown values can only come from localStorage drift or manual edits.
 * Falling back to `open` preserves backwards-compatible "rail starts open"
 * behavior for sessions with no saved open-state.
 */
export function normalizeWorkspacePanelDefault(
  value: string | null | undefined,
): WorkspacePanelDefault {
  return isWorkspacePanelDefault(value) ? value : WORKSPACE_PANEL_DEFAULT;
}

/**
 * Read the persisted default for new-chat Workspace rail visibility.
 *
 * Returns "open" when nothing is stored, on a server render (no `window`),
 * or when the stored value is missing/unknown — never throws, so a corrupt
 * entry can't break app boot.
 */
export function readWorkspacePanelDefault(): WorkspacePanelDefault {
  if (typeof window === "undefined") return WORKSPACE_PANEL_DEFAULT;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return WORKSPACE_PANEL_DEFAULT;
    return normalizeWorkspacePanelDefault(raw);
  } catch {
    return WORKSPACE_PANEL_DEFAULT;
  }
}

/**
 * Persist the default Workspace panel visibility for new chats. "open" clears
 * the key (the product default). Swallows quota/access errors so a failed
 * write can't break settings.
 */
export function writeWorkspacePanelDefault(value: WorkspacePanelDefault): void {
  if (typeof window === "undefined") return;
  try {
    const normalized = normalizeWorkspacePanelDefault(value);
    if (normalized === WORKSPACE_PANEL_DEFAULT) {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, normalized);
    }
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
}

/**
 * Boolean form of {@link readWorkspacePanelDefault} for AppShell's
 * `rightPanelOpen` fallback when a session has no saved `open` state.
 */
export function readDefaultWorkspacePanelOpen(): boolean {
  return readWorkspacePanelDefault() === "open";
}
