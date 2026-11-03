// A per-device on/off preference in localStorage that mounted readers follow,
// including writes from other windows. Only the non-default value is stored.

import { useSyncExternalStore } from "react";

export interface StoredBooleanPreference {
  readonly storageKey: string;
  readonly defaultValue: boolean;
  /** Read the preference; storage errors and unknown values mean the default. */
  read: () => boolean;
  /** Persist the preference and notify readers already on screen. */
  write: (value: boolean) => void;
  /** Re-read after a same-tab storage write that bypassed `write` (import, reset). */
  notifyChanged: () => void;
  /** Live value of the preference. */
  use: () => boolean;
}

export function createStoredBooleanPreference(
  storageKey: string,
  defaultValue: boolean,
): StoredBooleanPreference {
  const changeEvent = `${storageKey}-change`;
  const storedValue = String(!defaultValue);

  const read = () => {
    if (typeof window === "undefined") return defaultValue;
    try {
      return window.localStorage.getItem(storageKey) === storedValue ? !defaultValue : defaultValue;
    } catch {
      return defaultValue;
    }
  };

  const notifyChanged = () => {
    if (typeof window !== "undefined") window.dispatchEvent(new Event(changeEvent));
  };

  const write = (value: boolean) => {
    if (typeof window === "undefined") return;
    try {
      if (value === defaultValue) {
        window.localStorage.removeItem(storageKey);
      } else {
        window.localStorage.setItem(storageKey, storedValue);
      }
    } catch {
      // A storage failure must not break Settings.
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
