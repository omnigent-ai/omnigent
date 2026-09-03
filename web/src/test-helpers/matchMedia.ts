// One configurable `window.matchMedia` stub for jsdom suites.
//
// Evaluates the queries the shell actually asks — viewport width
// (`min-width` / `max-width`), pointer coarseness and hover capability,
// including `and`-joined compounds — against one declared environment, so
// every suite answers a new query the same way instead of each file carrying
// its own partial evaluator. Unknown conditions answer false.

export interface MediaEnvironment {
  /** Simulated viewport width in CSS px (default: a 1280px desktop). */
  width?: number;
  /** `(any-pointer: coarse)` — some attached pointer is coarse. */
  anyCoarse?: boolean;
  /** `(pointer: coarse)` — the PRIMARY pointer is coarse (defaults to `anyCoarse`). */
  coarsePrimary?: boolean;
  /** `(hover: hover)` — the primary pointer can hover. */
  canHover?: boolean;
  /** `(any-hover: hover)` — some pointer can hover (defaults to `canHover`). */
  anyHover?: boolean;
}

function evaluate(query: string, env: Required<MediaEnvironment>): boolean {
  return query.split(/\s+and\s+/).every((condition) => {
    const normalized = condition.trim();
    if (normalized === "(pointer: coarse)") return env.coarsePrimary;
    if (normalized === "(any-pointer: coarse)") return env.anyCoarse;
    if (normalized === "(hover: hover)") return env.canHover;
    if (normalized === "(any-hover: hover)") return env.anyHover;
    const min = normalized.match(/^\(min-width: ([\d.]+)px\)$/);
    if (min) return env.width >= parseFloat(min[1]);
    const max = normalized.match(/^\(max-width: ([\d.]+)px\)$/);
    if (max) return env.width <= parseFloat(max[1]);
    return false;
  });
}

/**
 * Install a `matchMedia` answering for `env`; returns a restore function.
 * Static: change listeners are accepted and never fired (suites that need
 * breakpoint-change events use `mockMatchMedia` from resizeHookTestHelpers).
 */
export function stubMatchMedia(env: MediaEnvironment = {}): () => void {
  const anyCoarse = env.anyCoarse ?? false;
  const canHover = env.canHover ?? false;
  const resolved: Required<MediaEnvironment> = {
    width: env.width ?? 1280,
    anyCoarse,
    coarsePrimary: env.coarsePrimary ?? anyCoarse,
    canHover,
    anyHover: env.anyHover ?? canHover,
  };
  const original = window.matchMedia;
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: evaluate(query, resolved),
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
  return () => {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      configurable: true,
      value: original,
    });
  };
}
