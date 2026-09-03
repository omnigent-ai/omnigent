// Shared jsdom scaffolding for the resizable-hook test suites. Each suite still
// captures and restores window.innerWidth / window.matchMedia itself; these are
// just the byte-identical helpers that several suites otherwise duplicated.

type MediaListener = (e: MediaQueryListEvent) => void;

export function setInnerWidth(px: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: px });
}

/** Controllable matchMedia mock: per-query matches plus a change-event firer. */
export function mockMatchMedia(matches: Record<string, boolean> = {}) {
  const listeners = new Map<string, Set<MediaListener>>();
  window.matchMedia = ((query: string) => ({
    matches: matches[query] ?? false,
    media: query,
    onchange: null,
    addEventListener: (_: string, cb: MediaListener) => {
      if (!listeners.has(query)) listeners.set(query, new Set());
      listeners.get(query)?.add(cb);
    },
    removeEventListener: (_: string, cb: MediaListener) => listeners.get(query)?.delete(cb),
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
  return {
    fire(query: string, value: boolean) {
      matches[query] = value;
      for (const cb of listeners.get(query) ?? new Set<MediaListener>()) {
        cb({ matches: value } as MediaQueryListEvent);
      }
    },
  };
}
