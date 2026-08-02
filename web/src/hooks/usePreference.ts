/**
 * React binding for a {@link LocalPreference}.
 *
 * Subscribes via `useSyncExternalStore` so Settings controls (and anything
 * else) re-render when the preference is written — including Appearance reset
 * — without a remount key.
 */

import { useCallback, useSyncExternalStore } from "react";
import type { LocalPreference } from "@/lib/preferences";

export type PreferenceSetter<T> = (value: T | ((prev: T) => T)) => void;

/**
 * Subscribe to a local preference. Returns `[value, setValue]` like useState.
 * `setValue` accepts a value or an updater; both go through `pref.write`.
 */
export function usePreference<T>(pref: LocalPreference<T>): [T, PreferenceSetter<T>] {
  const value = useSyncExternalStore(pref.subscribe, pref.read, () => pref.defaultValue);

  const setValue = useCallback<PreferenceSetter<T>>(
    (next) => {
      const resolved = typeof next === "function" ? (next as (prev: T) => T)(pref.read()) : next;
      pref.write(resolved);
    },
    [pref],
  );

  return [value, setValue];
}
