// Persisted, app-global preferences for resizable panel sizes.
//
// The resize hooks keep live sizes in module-level stores so panels do not
// jump while switching views. This file snapshots only explicit user choices
// to localStorage so a full page refresh restores the same layout.

export interface PanelSizePreferences {
  /** Shared width for right-side push panels such as file viewer/terminals. */
  pushPanelWidthPx?: number;
  /** Width for the always-visible desktop right rail. */
  inlinePanelWidthPx?: number;
  /** Width for the always-visible desktop left sidebar (conversations). */
  sidebarWidthPx?: number;
  /** Width for the comments panel beside the file viewer. */
  commentsPanelWidthPx?: number;
  /** Height for the comments panel when it stacks under the file viewer. */
  commentsPanelHeightPx?: number;
}

export type PanelSizePreferenceKey = keyof PanelSizePreferences;

const STORAGE_KEY = "omnigent:panel-size-preferences";

function isValidSize(value: unknown): value is number {
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
    if (isValidSize(record.pushPanelWidthPx)) prefs.pushPanelWidthPx = record.pushPanelWidthPx;
    if (isValidSize(record.inlinePanelWidthPx))
      prefs.inlinePanelWidthPx = record.inlinePanelWidthPx;
    if (isValidSize(record.sidebarWidthPx)) prefs.sidebarWidthPx = record.sidebarWidthPx;
    if (isValidSize(record.commentsPanelWidthPx))
      prefs.commentsPanelWidthPx = record.commentsPanelWidthPx;
    if (isValidSize(record.commentsPanelHeightPx))
      prefs.commentsPanelHeightPx = record.commentsPanelHeightPx;
    return prefs;
  } catch {
    return {};
  }
}

/**
 * Read one persisted panel size.
 *
 * @param key Preference field to read, e.g. ``"inlinePanelWidthPx"``.
 * @returns The stored pixel size, or ``null`` when absent/invalid.
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
 * Persist one panel size preference.
 *
 * @param key Preference field to write, e.g. ``"pushPanelWidthPx"``.
 * @param size Pixel size to store. ``null`` removes that field.
 */
export function writePanelSizePreference(key: PanelSizePreferenceKey, size: number | null): void {
  const prefs = readPanelSizePreferences();
  if (size === null) {
    const { [key]: _removedPreference, ...remainingPreferences } = prefs;
    writePanelSizePreferences(remainingPreferences);
    return;
  } else if (isValidSize(size)) {
    prefs[key] = size;
  } else {
    return;
  }
  writePanelSizePreferences(prefs);
}
