// A per-device value in localStorage that mounted readers follow, including
// writes from other windows. Only a non-default value is stored.

import { useSyncExternalStore } from "react";

export interface StoredPreference<Value extends string | number | boolean> {
  readonly storageKey: string;
  readonly defaultValue: Value;
  /** Read the preference; storage errors and unknown values mean the default. */
  read: () => Value;
  /** Persist the preference and notify readers already on screen. */
  write: (value: Value) => void;
  /** Re-read after a same-tab storage write that bypassed `write` (import, reset). */
  notifyChanged: () => void;
  /** Live value of the preference. */
  use: () => Value;
}

/** `parse` maps a stored string to its value, or to undefined when it names none. */
export function createStoredPreference<Value extends string | number | boolean>(
  storageKey: string,
  parse: (stored: string) => Value | undefined,
  defaultValue: NoInfer<Value>,
): StoredPreference<Value> {
  const changeEvent = `${storageKey}-change`;

  const read = () => {
    if (typeof window === "undefined") return defaultValue;
    try {
      const stored = window.localStorage.getItem(storageKey);
      return (stored === null ? undefined : parse(stored)) ?? defaultValue;
    } catch {
      return defaultValue;
    }
  };

  const notifyChanged = () => {
    if (typeof window !== "undefined") window.dispatchEvent(new Event(changeEvent));
  };

  const write = (value: Value) => {
    if (typeof window === "undefined") return;
    try {
      if (value === defaultValue) {
        window.localStorage.removeItem(storageKey);
      } else {
        window.localStorage.setItem(storageKey, String(value));
      }
    } catch {
      // Ignore storage failures (quota, privacy mode); the stored value is left as it was.
    }
    notifyChanged();
  };

  const subscribe = (listener: () => void) => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== null && event.key !== storageKey) return;
      try {
        if (event.storageArea !== null && event.storageArea !== window.localStorage) return;
      } catch {
        // Unreadable storage: re-read, which falls back to the default.
      }
      listener();
    };
    window.addEventListener(changeEvent, listener);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(changeEvent, listener);
      window.removeEventListener("storage", onStorage);
    };
  };

  const use = () => useSyncExternalStore(subscribe, read, () => defaultValue);

  return { storageKey, defaultValue, read, write, notifyChanged, use };
}

/** A preference holding one choice from a fixed list. */
export function createStoredChoicePreference<Choice extends string>(
  storageKey: string,
  choices: readonly Choice[],
  defaultValue: NoInfer<Choice>,
): StoredPreference<Choice> {
  return createStoredPreference(
    storageKey,
    (stored) => choices.find((choice) => choice === stored),
    defaultValue,
  );
}
