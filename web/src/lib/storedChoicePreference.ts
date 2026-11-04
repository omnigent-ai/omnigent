// A per-device choice in localStorage that mounted readers follow, including
// writes from other windows. Only a non-default choice is stored.

import { useSyncExternalStore } from "react";

export interface StoredChoicePreference<Choice extends string> {
  readonly storageKey: string;
  readonly defaultValue: Choice;
  /** Read the preference; storage errors and unknown values mean the default. */
  read: () => Choice;
  /** Persist the preference and notify readers already on screen. */
  write: (value: Choice) => void;
  /** Re-read after a same-tab storage write that bypassed `write` (import, reset). */
  notifyChanged: () => void;
  /** Live value of the preference. */
  use: () => Choice;
}

export function createStoredChoicePreference<Choice extends string>(
  storageKey: string,
  choices: readonly Choice[],
  defaultValue: NoInfer<Choice>,
): StoredChoicePreference<Choice> {
  const changeEvent = `${storageKey}-change`;

  const read = () => {
    if (typeof window === "undefined") return defaultValue;
    try {
      const stored = window.localStorage.getItem(storageKey);
      return choices.find((choice) => choice === stored) ?? defaultValue;
    } catch {
      return defaultValue;
    }
  };

  const notifyChanged = () => {
    if (typeof window !== "undefined") window.dispatchEvent(new Event(changeEvent));
  };

  const write = (value: Choice) => {
    if (typeof window === "undefined") return;
    try {
      if (value === defaultValue) {
        window.localStorage.removeItem(storageKey);
      } else {
        window.localStorage.setItem(storageKey, value);
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
