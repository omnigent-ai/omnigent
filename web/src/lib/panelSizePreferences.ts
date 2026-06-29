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
}

export type PanelSizePreferenceKey = keyof PanelSizePreferences;

const STORAGE_KEY = "omnigent:panel-size-preferences";

function isValidWidth(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
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
    if (isValidWidth(record.pushPanelWidthPx)) prefs.pushPanelWidthPx = record.pushPanelWidthPx;
    if (isValidWidth(record.inlinePanelWidthPx))
      prefs.inlinePanelWidthPx = record.inlinePanelWidthPx;
    if (isValidWidth(record.sidebarWidthPx)) prefs.sidebarWidthPx = record.sidebarWidthPx;
    if (isValidWidth(record.commentsPanelWidthPx))
      prefs.commentsPanelWidthPx = record.commentsPanelWidthPx;
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
    delete prefs[key];
  } else if (isValidWidth(width)) {
    prefs[key] = width;
  } else {
    return;
  }
  writePanelSizePreferences(prefs);
}
