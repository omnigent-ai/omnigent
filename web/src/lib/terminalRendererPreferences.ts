// Persisted preference for the terminal's xterm.js renderer. `auto` loads the
// WebGL addon (fast, but its glyph texture atlas is shared across every mounted
// terminal and can corrupt long sessions); `dom` skips it so xterm falls back to
// the DOM renderer. Applied imperatively per terminal, so a mid-session change
// is pushed to mounted terminals via a pub/sub.

/** localStorage key holding the renderer mode; exported for settings export/import. */
export const TERMINAL_RENDERER_STORAGE_KEY = "omnigent:terminal-renderer";

export const terminalRendererModes = ["auto", "dom"] as const;
export type TerminalRendererMode = (typeof terminalRendererModes)[number];
export const TERMINAL_RENDERER_DEFAULT: TerminalRendererMode = "auto";

/** Return whether a string is one of the selectable terminal renderer modes. */
export function isTerminalRendererMode(
  value: string | null | undefined,
): value is TerminalRendererMode {
  return value === "auto" || value === "dom";
}

/**
 * Normalize a stored terminal renderer string to the default auto mode.
 *
 * Unknown values can only come from localStorage drift or manual edits.
 * Falling back to `auto` matches the documented default and preserves
 * backwards-compatible "use WebGL when available" behavior.
 */
export function normalizeTerminalRendererMode(
  value: string | null | undefined,
): TerminalRendererMode {
  return isTerminalRendererMode(value) ? value : TERMINAL_RENDERER_DEFAULT;
}

/**
 * Read the persisted terminal renderer mode.
 *
 * Returns "auto" when nothing is stored, on a server render (no `window`),
 * or when the stored value is missing/unknown — never throws, so a corrupt
 * entry can't break app boot.
 */
export function readTerminalRendererMode(): TerminalRendererMode {
  if (typeof window === "undefined") return TERMINAL_RENDERER_DEFAULT;
  try {
    const raw = window.localStorage.getItem(TERMINAL_RENDERER_STORAGE_KEY);
    if (!raw) return TERMINAL_RENDERER_DEFAULT;
    return normalizeTerminalRendererMode(raw);
  } catch {
    return TERMINAL_RENDERER_DEFAULT;
  }
}

/**
 * Persist the terminal renderer mode, then notify subscribers so mounted
 * terminals swap renderer live. "auto" clears the key (the default). Swallows
 * quota/access errors so a failed write can't break the app.
 */
export function writeTerminalRendererMode(mode: TerminalRendererMode): void {
  const normalized = normalizeTerminalRendererMode(mode);
  if (typeof window !== "undefined") {
    try {
      if (normalized === TERMINAL_RENDERER_DEFAULT) {
        window.localStorage.removeItem(TERMINAL_RENDERER_STORAGE_KEY);
      } else {
        window.localStorage.setItem(TERMINAL_RENDERER_STORAGE_KEY, normalized);
      }
    } catch {
      // localStorage quota or access errors shouldn't break the app.
    }
  }
  // Broadcast the intended value, not a storage re-read: if the write above
  // failed (quota/denied), mounted terminals must still swap renderer now
  // rather than staying on the stale/default stored value.
  emit(normalized);
}

/** Resolve whether the WebGL addon should be loaded for the given mode. */
export function resolveTerminalWebglEnabled(mode: TerminalRendererMode): boolean {
  switch (mode) {
    case "auto":
      return true;
    case "dom":
      return false;
    default: {
      const exhaustive: never = mode;
      return exhaustive;
    }
  }
}

type TerminalRendererListener = (mode: TerminalRendererMode) => void;

const listeners = new Set<TerminalRendererListener>();

/**
 * Subscribe to terminal renderer changes. The callback fires with the current
 * {@link TerminalRendererMode} whenever it is written (e.g. from Settings),
 * letting an already-mounted terminal swap renderer without a reload. Returns
 * an unsubscribe function.
 */
export function subscribeTerminalRenderer(listener: TerminalRendererListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Notify subscribers of the given terminal renderer mode. Called after every write. */
function emit(mode: TerminalRendererMode): void {
  for (const listener of listeners) listener(mode);
}
