// Persisted, per-device set of picker entries the user has hidden.
//
// The "hide unconfigured harnesses" preference next door filters on *host
// readiness* — it can only drop rows that cannot launch. This one is the
// user's own choice: any harness or agent row, including ones that are
// perfectly launchable, and including the bundle agents (Polly / Debby) that
// readiness filtering never touches.
//
// Entries are keyed by agent NAME, not id. A built-in's id is name-derived and
// stable, but a user-registered template gets a fresh random id every time it
// is re-registered — hiding by id would silently un-hide it. The name is also
// the unit the server-side OMNIGENT_SEEDED_AGENTS allowlist works in, so the
// two stay legible together.
//
// Unlike the read-once boolean preference, this is exposed as a subscribable
// store: the Settings switch and an open picker are mounted at the same time,
// and a per-entry list gets toggled far more often than a one-time flag, so it
// has to update without a page reload.

export const HIDDEN_PICKER_AGENTS_STORAGE_KEY = "omnigent:hidden-picker-agents";

const EMPTY: ReadonlySet<string> = new Set();

// Cached snapshot. useSyncExternalStore compares snapshots by reference and
// re-renders in a loop if a new Set is handed back on every call, so the parsed
// value is memoized and only replaced when the stored string actually changes.
let cachedRaw: string | null = null;
let cachedValue: ReadonlySet<string> = EMPTY;

const listeners = new Set<() => void>();

function parse(raw: string | null): ReadonlySet<string> {
  if (!raw) return EMPTY;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return EMPTY;
    const names = parsed.filter((x): x is string => typeof x === "string");
    return names.length > 0 ? new Set(names) : EMPTY;
  } catch {
    // A corrupt entry must not break the picker — treat it as "nothing hidden".
    return EMPTY;
  }
}

/**
 * Read the hidden-entry set.
 *
 * Returns a stable reference while the stored value is unchanged, so it is
 * safe as a ``useSyncExternalStore`` snapshot.
 *
 * @returns Agent names the user has hidden from the picker.
 */
export function readHiddenPickerAgents(): ReadonlySet<string> {
  if (typeof window === "undefined") return EMPTY;
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(HIDDEN_PICKER_AGENTS_STORAGE_KEY);
  } catch {
    return EMPTY;
  }
  if (raw !== cachedRaw) {
    cachedRaw = raw;
    cachedValue = parse(raw);
  }
  return cachedValue;
}

/**
 * Persist the hidden-entry set and notify subscribers.
 *
 * An empty set removes the key rather than storing ``[]``, so an untouched
 * preference stays absent from the settings export.
 *
 * @param names - Agent names to hide.
 */
export function writeHiddenPickerAgents(names: ReadonlySet<string>): void {
  if (typeof window === "undefined") return;
  try {
    if (names.size === 0) {
      window.localStorage.removeItem(HIDDEN_PICKER_AGENTS_STORAGE_KEY);
    } else {
      window.localStorage.setItem(
        HIDDEN_PICKER_AGENTS_STORAGE_KEY,
        JSON.stringify([...names].sort()),
      );
    }
  } catch {
    // Quota or access errors are non-fatal; the toggle just doesn't persist.
  }
  for (const listener of listeners) listener();
}

/**
 * Subscribe to hidden-entry changes, including writes from another tab.
 *
 * @param listener - Called after every change.
 * @returns An unsubscribe function.
 */
export function subscribeHiddenPickerAgents(listener: () => void): () => void {
  listeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === HIDDEN_PICKER_AGENTS_STORAGE_KEY) listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

/**
 * Toggle one entry's visibility.
 *
 * @param name - The agent name to show or hide.
 * @param hidden - ``true`` to hide it, ``false`` to show it.
 */
export function setPickerAgentHidden(name: string, hidden: boolean): void {
  const current = readHiddenPickerAgents();
  if (hidden === current.has(name)) return;
  const next = new Set(current);
  if (hidden) next.add(name);
  else next.delete(name);
  writeHiddenPickerAgents(next);
}

/** Clear every hidden entry, restoring the full picker. */
export function resetHiddenPickerAgents(): void {
  writeHiddenPickerAgents(EMPTY);
}
