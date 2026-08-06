// Shared reactive media-query subscription. Each distinct query string gets
// ONE module-level MediaQueryList and one native "change" listener fanning out
// to a shared listener set — so many subscribers (e.g. every sidebar row) cost
// a single matchMedia registration instead of one each.

import { useSyncExternalStore } from "react";

interface MediaQueryStore {
  subscribe: (onChange: () => void) => () => void;
  getSnapshot: () => boolean;
}

const stores = new Map<string, MediaQueryStore>();

function createStore(query: string): MediaQueryStore {
  const listeners = new Set<() => void>();
  // The native registration is created lazily (SSR-safe import) and kept for
  // the app's lifetime; queries are static, so this never accumulates.
  let registered = false;
  return {
    subscribe(onChange) {
      if (!registered && typeof window !== "undefined" && window.matchMedia) {
        registered = true;
        window.matchMedia(query).addEventListener("change", () => {
          for (const listener of listeners) listener();
        });
      }
      listeners.add(onChange);
      return () => listeners.delete(onChange);
    },
    getSnapshot() {
      // Read fresh rather than off a cached MediaQueryList: `matches` is a
      // cheap live getter, and tests stub window.matchMedia per case.
      if (typeof window === "undefined" || !window.matchMedia) return false;
      return window.matchMedia(query).matches;
    },
  };
}

function storeFor(query: string): MediaQueryStore {
  let store = stores.get(query);
  if (store === undefined) {
    store = createStore(query);
    stores.set(query, store);
  }
  return store;
}

/**
 * True while `query` matches. Reactive (re-renders on change) and SSR-safe
 * (returns `false` on the server). Subscriptions for the same query share one
 * MediaQueryList and one native listener.
 */
export function useMediaQuery(query: string): boolean {
  const store = storeFor(query);
  return useSyncExternalStore(store.subscribe, store.getSnapshot, () => false);
}
