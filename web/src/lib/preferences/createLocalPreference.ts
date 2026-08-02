/**
 * Declarative localStorage preference factory.
 *
 * One call owns the storage key, default, parse/serialize, validation,
 * change subscription, and optional Appearance-reset registration — so adding
 * a setting is a declaration instead of a multi-file recipe.
 *
 * Migrate remaining `*Preferences.ts` modules by wrapping them with
 * {@link createLocalPreference}, setting `appearance: true` when the setting
 * belongs in Appearance → Reset, then wiring it in `appearancePrefs.ts`
 * (barrel import + `EXPECTED_APPEARANCE_STORAGE_KEYS`, remove from
 * `LEGACY_APPEARANCE_STORAGE_KEYS`). Keep the
 * same `key` (or supply an explicit `parse` migration) so persisted values
 * survive the upgrade.
 */

import { registerAppearancePreference } from "./appearanceRegistry";

export type LocalPreferenceOptions<T> = {
  /** Stable localStorage key. Changing it resets users unless `parse` migrates. */
  key: string;
  defaultValue: T;
  /**
   * Turn a stored string into T. Called only when the key exists. Must not
   * throw — return `defaultValue` (or a migrated value) for corrupt input.
   */
  parse: (raw: string) => T;
  /** Turn T into the string written to localStorage. */
  serialize: (value: T) => string;
  /** Optional clamp/normalize applied after parse and before write. */
  normalize?: (value: T) => T;
  /**
   * When true, writing the default removes the key instead of storing it.
   * Matches enum prefs that treat "no key" as the product default.
   */
  clearWhenDefault?: boolean;
  /** Side effect after every write/reset (CSS vars, imperative widgets, …). */
  onChange?: (value: T) => void;
  /**
   * Register with the Appearance reset registry so Settings reset stays in
   * sync with the definition — no second hand-maintained key list.
   */
  appearance?: boolean;
};

export type LocalPreference<T> = {
  readonly key: string;
  readonly defaultValue: T;
  read: () => T;
  write: (value: T) => void;
  /** Clear the key and notify with `defaultValue` (Appearance reset path). */
  reset: () => void;
  /** `useSyncExternalStore`-compatible subscribe (no value arg). */
  subscribe: (onStoreChange: () => void) => () => void;
  /** Value-carrying subscribe for imperative listeners (editors, terminals). */
  subscribeValue: (listener: (value: T) => void) => () => void;
};

function identity<T>(value: T): T {
  return value;
}

/**
 * Create a typed localStorage preference with pub/sub.
 *
 * Reads never throw (corrupt storage → default). Writes swallow quota/access
 * errors but still notify listeners with the intended value so the UI can
 * update even when persistence fails.
 */
export function createLocalPreference<T>(options: LocalPreferenceOptions<T>): LocalPreference<T> {
  const {
    key,
    defaultValue,
    parse,
    serialize,
    normalize = identity,
    clearWhenDefault = false,
    onChange,
    appearance = false,
  } = options;

  const storeListeners = new Set<() => void>();
  const valueListeners = new Set<(value: T) => void>();

  const read = (): T => {
    if (typeof window === "undefined") return defaultValue;
    try {
      const raw = window.localStorage.getItem(key);
      if (raw === null) return defaultValue;
      return normalize(parse(raw));
    } catch {
      return defaultValue;
    }
  };

  const emit = (value: T): void => {
    for (const listener of storeListeners) listener();
    for (const listener of valueListeners) listener(value);
    onChange?.(value);
  };

  const persist = (value: T): void => {
    if (typeof window === "undefined") return;
    try {
      if (clearWhenDefault && Object.is(value, defaultValue)) {
        window.localStorage.removeItem(key);
      } else {
        window.localStorage.setItem(key, serialize(value));
      }
    } catch {
      // localStorage quota or access errors shouldn't break the app.
    }
  };

  const write = (value: T): void => {
    const next = normalize(value);
    persist(next);
    // Broadcast the intended value, not a storage re-read: if persist failed,
    // subscribers must still see the new value rather than the stale/default.
    emit(next);
  };

  const reset = (): void => {
    if (typeof window !== "undefined") {
      try {
        window.localStorage.removeItem(key);
      } catch {
        // localStorage access errors are non-fatal.
      }
    }
    emit(defaultValue);
  };

  const subscribe = (onStoreChange: () => void): (() => void) => {
    storeListeners.add(onStoreChange);
    return () => {
      storeListeners.delete(onStoreChange);
    };
  };

  const subscribeValue = (listener: (value: T) => void): (() => void) => {
    valueListeners.add(listener);
    return () => {
      valueListeners.delete(listener);
    };
  };

  const preference: LocalPreference<T> = {
    key,
    defaultValue,
    read,
    write,
    reset,
    subscribe,
    subscribeValue,
  };

  if (appearance) {
    // Registry only needs key + reset; pass a narrow object to avoid T→unknown casts.
    registerAppearancePreference({ key: preference.key, reset: preference.reset });
  }

  return preference;
}
