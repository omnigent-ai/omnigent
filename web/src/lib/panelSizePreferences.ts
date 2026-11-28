// Persisted, app-global preferences for resizable panel widths.
//
// The resize hooks keep live width in module-level stores so panels do not
// jump while switching views. This file snapshots only explicit user choices
// to localStorage so a full page refresh restores the same layout.

export interface PanelSizePreferences {
  /** Shared width for right-side push panels such as file viewer/terminals. */
  pushPanelWidthPx?: number;
  /** Width for the always-visible desktop right rail. */
  inlinePanelWidthPx?: number;
  /** Width for the always-visible desktop left sidebar (conversations). */
  sidebarWidthPx?: number;
  /** Width for the comments panel inside the file viewer. */
  commentsPanelWidthPx?: number;
  /** Width for the session panel beside every project's board. */
  projectSessionPanelWidthPx?: number;
  /** Width for the list of pull requests beside one in the workspace. */
  pullRequestListWidthPx?: number;
  /** Width for the Files tree docked beside a file in the workspace. */
  filesTreeWidthPx?: number;
  /** The Files tree's share of the page it's docked in, between 0 and 1. */
  filesTreeShare?: number;
  /** The share of a session's Changes review that its file tree takes. */
  changesReviewTreeShare?: number;
  /** The share of a pull request's Changes review that its file tree takes. */
  pullRequestReviewTreeShare?: number;
}

const WIDTH_KEYS = [
  "pushPanelWidthPx",
  "inlinePanelWidthPx",
  "sidebarWidthPx",
  "commentsPanelWidthPx",
  "projectSessionPanelWidthPx",
  "pullRequestListWidthPx",
  "filesTreeWidthPx",
] as const satisfies readonly (keyof PanelSizePreferences)[];

const SHARE_KEYS = [
  "filesTreeShare",
  "changesReviewTreeShare",
  "pullRequestReviewTreeShare",
] as const satisfies readonly (keyof PanelSizePreferences)[];

/** A panel remembered as a pixel width. */
export type PanelSizePreferenceKey = (typeof WIDTH_KEYS)[number];
/** A panel remembered as its share of the container it's in. */
export type PanelSharePreferenceKey = (typeof SHARE_KEYS)[number];

const STORAGE_KEY = "omnigent:panel-size-preferences";

function isValidWidth(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function isValidShare(value: unknown): value is number {
  return isValidWidth(value) && value < 1;
}

/**
 * Read all persisted panel size preferences.
 *
 * Returns an empty object when storage is unavailable or malformed. Fields are
 * validated independently so a bad value for one panel cannot discard the
 * others.
 */
export function readPanelSizePreferences(): PanelSizePreferences {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
    const record = parsed as Record<string, unknown>;
    const prefs: PanelSizePreferences = {};
    for (const key of WIDTH_KEYS) {
      const value = record[key];
      if (isValidWidth(value)) prefs[key] = value;
    }
    for (const key of SHARE_KEYS) {
      const value = record[key];
      if (isValidShare(value)) prefs[key] = value;
    }
    return prefs;
  } catch {
    return {};
  }
}

/**
 * Read one persisted panel width.
 *
 * @param key Preference field to read, e.g. ``"inlinePanelWidthPx"``.
 * @returns The stored pixel width, or ``null`` when absent/invalid.
 */
export function readPanelSizePreference(key: PanelSizePreferenceKey): number | null {
  return readPanelSizePreferences()[key] ?? null;
}

function writePanelSizePreferences(prefs: PanelSizePreferences): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // Storage quota/access errors should not break resize interactions.
  }
}

/**
 * Persist one panel width preference.
 *
 * @param key Preference field to write, e.g. ``"pushPanelWidthPx"``.
 * @param width Pixel width to store. ``null`` removes that field.
 */
export function writePanelSizePreference(key: PanelSizePreferenceKey, width: number | null): void {
  const prefs = readPanelSizePreferences();
  if (width === null) {
    const { [key]: _removedPreference, ...remainingPreferences } = prefs;
    writePanelSizePreferences(remainingPreferences);
    return;
  } else if (isValidWidth(width)) {
    prefs[key] = width;
  } else {
    return;
  }
  writePanelSizePreferences(prefs);
}

/** One panel's remembered share of its container, or null when absent or invalid. */
export function readPanelSharePreference(key: PanelSharePreferenceKey): number | null {
  return readPanelSizePreferences()[key] ?? null;
}

/** Remember one panel's share of its container, between 0 and 1; null removes it. */
export function writePanelSharePreference(
  key: PanelSharePreferenceKey,
  share: number | null,
): void {
  const prefs = readPanelSizePreferences();
  if (share === null) {
    const { [key]: _removedPreference, ...remainingPreferences } = prefs;
    writePanelSizePreferences(remainingPreferences);
    return;
  }
  if (!isValidShare(share)) return;
  prefs[key] = share;
  writePanelSizePreferences(prefs);
}
