// Persisted, app-global preferences for how the file viewer renders files.
//
// These are *preferences*, not per-file or per-session state: the diff
// on/off toggle, the split/unified layout, and the source/preview mode all
// "carry over" as the user navigates between files (see FileViewer's
// "Diff is a global toggle" comment), and should also survive a page
// refresh. They're stored under a single localStorage key with no
// per-conversation keying, mirroring that global-toggle semantics.
//
// FileViewer keeps the live React state as the source of truth for the UI;
// these helpers only seed that state on mount and snapshot it on change, so
// a refresh (or a brand-new conversation) starts from the user's last
// choice instead of the hardcoded defaults.

export interface FileViewPreferences {
  /** Whether the diff view is the preferred mode for changed files. */
  diffActive: boolean;
  /** How diff hunks render: inline ("unified") or side-by-side ("split"). */
  diffLayout: "unified" | "split";
  /** Preferred mode for previewable (markdown/html) files. */
  previewableViewMode: "editor" | "preview" | "source";
}

const STORAGE_KEY = "omnigent:file-view-preferences";

// Bump when a DEFAULT changes such that a value auto-persisted under the old
// default must NOT keep pinning returning users to it. The file viewer writes
// its seeded state back on mount (idempotent persist), so every user who ever
// opened a file already has a `previewableViewMode` in storage even if they
// never touched a toggle.
//
// v2 (issue #970): the previewable default flipped "editor" → "preview". A
// record without `v === 2` therefore can't distinguish a deliberate "editor"
// choice from the old auto-written default, so we ignore its previewableViewMode
// and fall back to the new "preview" default (diff prefs are still honored).
const SCHEMA_VERSION = 2;

export const DEFAULT_FILE_VIEW_PREFERENCES: FileViewPreferences = {
  diffActive: false,
  diffLayout: "unified",
  // Previewable files (markdown/html) open in the rendered preview pane by
  // default (issue #970). Markdown's editable rich-text view and raw source
  // are one toolbar tap away; HTML toggles preview ↔ source.
  previewableViewMode: "preview",
};

/**
 * Read the persisted file-view preferences. Returns the defaults when
 * nothing is stored, on a server render (no `window`), or when the stored
 * value is malformed — never throws, so a corrupt entry can't break the app.
 * Each field is validated independently so a partial/garbage record still
 * yields sane values for the fields that are valid.
 */
export function readFileViewPreferences(): FileViewPreferences {
  if (typeof window === "undefined") return DEFAULT_FILE_VIEW_PREFERENCES;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_FILE_VIEW_PREFERENCES;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return DEFAULT_FILE_VIEW_PREFERENCES;
    }
    const p = parsed as Record<string, unknown>;
    // Only trust a stored previewableViewMode written by the CURRENT schema —
    // a pre-v2 record's value was the old hardcoded default, not a real choice.
    const isCurrentSchema = p.v === SCHEMA_VERSION;
    return {
      diffActive:
        typeof p.diffActive === "boolean" ? p.diffActive : DEFAULT_FILE_VIEW_PREFERENCES.diffActive,
      diffLayout: p.diffLayout === "split" ? "split" : "unified",
      previewableViewMode:
        isCurrentSchema &&
        (p.previewableViewMode === "preview" ||
          p.previewableViewMode === "editor" ||
          p.previewableViewMode === "source")
          ? p.previewableViewMode
          : "preview",
    };
  } catch {
    return DEFAULT_FILE_VIEW_PREFERENCES;
  }
}

/**
 * Persist the file-view preferences. Swallows quota/access errors so a
 * failed write can't break the viewer.
 */
export function writeFileViewPreferences(prefs: FileViewPreferences): void {
  if (typeof window === "undefined") return;
  try {
    // Stamp the schema version so a future read can tell a deliberately-chosen
    // mode from a value auto-written under an older default (see SCHEMA_VERSION).
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...prefs, v: SCHEMA_VERSION }));
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}
