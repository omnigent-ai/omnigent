// Persisted, app-global preference for whether a brand-new chat's right
// Workspace rail (Files / Agents / Shells) starts open or collapsed.
//
// This only seeds sessions that have no saved per-chat `open` state. Once a
// user toggles the rail in a session, that session's own
// `SessionWorkspaceState.open` wins on restore. Set from Appearance settings.
//
// Owned by {@link createLocalPreference}; storage key and raw-string format
// are unchanged so existing localStorage values keep working.

import { createLocalPreference } from "@/lib/preferences";

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
 * Declarative Workspace panel default. Same key and raw `"open"`/`"collapsed"`
 * string format as before — no migration rewrite of stored values.
 */
export const workspacePanelPreference = createLocalPreference<WorkspacePanelDefault>({
  key: "omnigent:default-workspace-panel",
  defaultValue: WORKSPACE_PANEL_DEFAULT,
  parse: (raw) => normalizeWorkspacePanelDefault(raw),
  serialize: (value) => value,
  normalize: normalizeWorkspacePanelDefault,
  clearWhenDefault: true,
  appearance: true,
});

/** Read the persisted default for new-chat Workspace rail visibility. */
export function readWorkspacePanelDefault(): WorkspacePanelDefault {
  return workspacePanelPreference.read();
}

/** Persist the default Workspace panel visibility for new chats. */
export function writeWorkspacePanelDefault(value: WorkspacePanelDefault): void {
  workspacePanelPreference.write(value);
}

/**
 * Boolean form of {@link readWorkspacePanelDefault} for AppShell's
 * `rightPanelOpen` fallback when a session has no saved `open` state.
 */
export function readDefaultWorkspacePanelOpen(): boolean {
  return readWorkspacePanelDefault() === "open";
}
