// Browser-local fallback for the selected tab in a session's Workspace rail.
// A session's remembered tab and file-opening signals take precedence.

import {
  resetSessionWorkspaceTabSelections,
  unpinSessionWorkspaceTabs,
} from "./sessionWorkspaceState";

const STORAGE_KEY = "omnigent:default-workspace-tab";
export const PINNED_WORKSPACE_TABS_STORAGE_KEY = "omnigent:pinned-workspace-tabs";

export const defaultWorkspaceTabs = ["files", "changes", "github", "subagents"] as const;
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

/** Apply a changed default to existing chats; subsequent manual tab choices are remembered. */
export function writeDefaultWorkspaceTab(value: DefaultWorkspaceTab): void {
  if (typeof window === "undefined") return;
  try {
    const normalized = normalizeDefaultWorkspaceTab(value);
    const previous = readDefaultWorkspaceTab();
    if (normalized === DEFAULT_WORKSPACE_TAB) {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, normalized);
    }
    if (normalized !== previous) resetSessionWorkspaceTabSelections();
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
}

/** Built-in pages stay pinned unless the user chooses otherwise on this device. */
export function readPinnedWorkspaceTabs(): DefaultWorkspaceTab[] {
  try {
    const raw = window.localStorage.getItem(PINNED_WORKSPACE_TABS_STORAGE_KEY);
    if (raw !== null) {
      const value: unknown = JSON.parse(raw);
      if (Array.isArray(value)) {
        return defaultWorkspaceTabs.filter((tab) => value.includes(tab));
      }
    }
  } catch {
    // Missing or inaccessible storage keeps the product defaults.
  }
  return [...defaultWorkspaceTabs];
}

export function writePinnedWorkspaceTabs(tabs: readonly DefaultWorkspaceTab[]): void {
  try {
    const next = defaultWorkspaceTabs.filter((tab) => tabs.includes(tab));
    const previous = readPinnedWorkspaceTabs();
    if (next.length === defaultWorkspaceTabs.length) {
      window.localStorage.removeItem(PINNED_WORKSPACE_TABS_STORAGE_KEY);
    } else {
      window.localStorage.setItem(PINNED_WORKSPACE_TABS_STORAGE_KEY, JSON.stringify(next));
    }
    unpinSessionWorkspaceTabs(previous.filter((tab) => !next.includes(tab)));
  } catch {
    // Storage failures must not break settings.
  }
}

/** Unpinned pages only open after an explicit selection or deep link. */
export function readInitialWorkspaceTab(): DefaultWorkspaceTab | null {
  const pinned = readPinnedWorkspaceTabs();
  const preferred = readDefaultWorkspaceTab();
  return pinned.includes(preferred) ? preferred : (pinned[0] ?? null);
}
