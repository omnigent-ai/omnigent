import { useSyncExternalStore } from "react";

/**
 * A small per-device JSON preference in localStorage, shared across components
 * and windows. `parse` validates stored data; unreadable values read as `fallback`.
 */
export function createLocalJsonStore<T>(key: string, fallback: T, parse: (raw: unknown) => T) {
  const listeners = new Set<() => void>();
  let cache: { raw: string | null; value: T } = { raw: null, value: fallback };

  function read(): T {
    let raw: string | null;
    try {
      raw = localStorage.getItem(key);
    } catch {
      return fallback;
    }
    if (raw === cache.raw) return cache.value;
    let value = fallback;
    try {
      value = raw === null ? fallback : parse(JSON.parse(raw));
    } catch {
      // A corrupt entry reads as the fallback.
    }
    cache = { raw, value };
    return value;
  }

  function write(value: T): void {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // Storage unavailable: the value still applies until reload.
      cache = { raw: cache.raw, value };
    }
    for (const listener of listeners) listener();
  }

  function subscribe(listener: () => void) {
    listeners.add(listener);
    const onStorage = (event: StorageEvent) => event.key === key && listener();
    window.addEventListener("storage", onStorage);
    return () => {
      listeners.delete(listener);
      window.removeEventListener("storage", onStorage);
    };
  }

  return { read, write, use: () => useSyncExternalStore(subscribe, read) };
}
