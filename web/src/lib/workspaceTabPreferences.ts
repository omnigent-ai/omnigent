// Browser-local fallback for the selected tab in a session's Workspace rail.
// A session's remembered tab and file-opening signals take precedence.

const STORAGE_KEY = "omnigent:default-workspace-tab";

export const defaultWorkspaceTabs = [
  "files",
  "subagents",
  "terminals",
  "todos",
  "browser",
] as const;
export type DefaultWorkspaceTab = (typeof defaultWorkspaceTabs)[number];

/** Match today's product default: sessions without remembered state open Files. */
export const DEFAULT_WORKSPACE_TAB: DefaultWorkspaceTab = "files";

/** Return whether a string is one of the selectable default Workspace tabs. */
export function isDefaultWorkspaceTab(
  value: string | null | undefined,
): value is DefaultWorkspaceTab {
  return defaultWorkspaceTabs.some((tab) => tab === value);
}

/** Normalize unknown persisted values to the backwards-compatible Files default. */
export function normalizeDefaultWorkspaceTab(
  value: string | null | undefined,
): DefaultWorkspaceTab {
  return isDefaultWorkspaceTab(value) ? value : DEFAULT_WORKSPACE_TAB;
}

/** Read the default Workspace tab without letting storage failures break app boot. */
export function readDefaultWorkspaceTab(): DefaultWorkspaceTab {
  if (typeof window === "undefined") return DEFAULT_WORKSPACE_TAB;
  try {
    return normalizeDefaultWorkspaceTab(window.localStorage.getItem(STORAGE_KEY));
  } catch {
    return DEFAULT_WORKSPACE_TAB;
  }
}

/** Persist the selected fallback; Files clears the key because it is the product default. */
export function writeDefaultWorkspaceTab(value: DefaultWorkspaceTab): void {
  if (typeof window === "undefined") return;
  try {
    const normalized = normalizeDefaultWorkspaceTab(value);
    if (normalized === DEFAULT_WORKSPACE_TAB) {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, normalized);
    }
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
}
