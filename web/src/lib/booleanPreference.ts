// A per-device on/off preference in localStorage that mounted readers follow,
// including writes made in other windows.

import { useSyncExternalStore } from "react";

export interface BooleanPreference {
  storageKey: string;
  defaultValue: boolean;
  /** Read the preference; storage errors and unknown values mean the default. */
  read: () => boolean;
  /** Persist the preference and notify readers already on screen. */
  write: (enabled: boolean) => void;
  /** Tell readers on screen to re-read, after writing the key directly. */
  notify: () => void;
  /** Live value of the preference, for components. */
  use: () => boolean;
}

/**
 * Create a boolean preference stored as `"true"`/`"false"` under `storageKey`.
 * The default value is stored as an absent key, so a device with no override
 * keeps following the product default.
 *
 * :param storageKey: localStorage key, e.g. ``"omnigent:fold-assistant-narration"``.
 * :param defaultValue: Value when nothing (or an unknown value) is stored.
 */
export function createBooleanPreference(
  storageKey: string,
  defaultValue: boolean,
): BooleanPreference {
  const changeEvent = `${storageKey}-change`;
  const stored = String(!defaultValue);

  const read = (): boolean => {
    if (typeof window === "undefined") return defaultValue;
    try {
      return window.localStorage.getItem(storageKey) === stored ? !defaultValue : defaultValue;
    } catch {
      return defaultValue;
    }
  };

  const notify = (): void => {
    if (typeof window !== "undefined") window.dispatchEvent(new Event(changeEvent));
  };

  const write = (enabled: boolean): void => {
    if (typeof window === "undefined") return;
    try {
      if (enabled === defaultValue) {
        window.localStorage.removeItem(storageKey);
      } else {
        window.localStorage.setItem(storageKey, String(enabled));
      }
    } catch {
      // Ignore storage failures; readers fall back to the default.
    }
    notify();
  };

  const subscribe = (listener: () => void): (() => void) => {
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

  const use = (): boolean => useSyncExternalStore(subscribe, read, () => defaultValue);

  return { storageKey, defaultValue, read, write, notify, use };
}
