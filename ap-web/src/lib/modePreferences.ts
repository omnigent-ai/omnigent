// Persisted, app-global preference for the last mode the user picked on the
// new-session landing composer's Advanced menu, keyed by harness.
//
// The "mode" is harness-specific: Claude Code's permission mode, Codex's /
// OpenCode's approval mode, and Cursor's execution mode are distinct knobs
// living on distinct native harnesses. We store them under one JSON map
// (harness id -> mode value) so each harness remembers its own last pick and
// a new session seeds the Advanced menu from it instead of always starting on
// the harness default.
//
// Like agentPreferences, the landing screen keeps live React state as the
// source of truth; these helpers only snapshot a pick and seed it back on a
// later visit. The consumer validates the stored value against the harness's
// current mode list and falls back to the default when it no longer exists.

const STORAGE_KEY = "omnigent:last-mode-by-harness";

type ModeMap = Record<string, string>;

function readMap(): ModeMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    // Keep only string->string entries; tolerate a corrupted/partial blob.
    const out: ModeMap = {};
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v === "string") out[k] = v;
    }
    return out;
  } catch {
    return {};
  }
}

/**
 * Read the last mode the user picked for `harness` on the landing composer.
 * Returns `null` when nothing is stored, on a server render (no `window`),
 * or when storage is inaccessible/corrupted — never throws.
 */
export function readLastModeForHarness(harness: string | null | undefined): string | null {
  if (!harness) return null;
  return readMap()[harness] ?? null;
}

/**
 * Persist `mode` as the user's last explicit pick for `harness`. Swallows
 * quota/access errors so a failed write can't break session creation.
 */
export function writeLastModeForHarness(harness: string | null | undefined, mode: string): void {
  if (typeof window === "undefined" || !harness) return;
  try {
    const map = readMap();
    map[harness] = mode;
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // localStorage quota or access errors shouldn't break the composer.
  }
}
