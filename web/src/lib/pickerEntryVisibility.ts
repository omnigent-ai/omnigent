// Set of picker entries the user hid, stored in this browser. Like the other
// Appearance preferences, it reaches another device only through settings
// export and import.
//
// Keyed by agent NAME, not id: a re-registered template gets a fresh id, so
// hiding by id would silently un-hide it. Subscribable, because the Settings
// checklist and an open picker are mounted at the same time.

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
 * Tell subscribers the stored set changed underneath them.
 *
 * Writing through :func:`writeHiddenPickerAgents` notifies on its own. This is
 * for the paths that rewrite ``localStorage`` wholesale — importing settings,
 * resetting Appearance — because a same-tab write fires no ``storage`` event,
 * so a picker mounted beside Settings would keep the old set until it
 * remounted.
 */
export function notifyHiddenPickerAgentsChanged(): void {
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
