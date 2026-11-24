// Transient editor guards for routing consumers and BrowserRouter's Back/Forward events.
const guards = new Map<symbol, { confirm: () => boolean; scope?: string }>();
let acceptedActions = 0;
let lastIndex: number | null = null;
let expectedIndex: number | null = null;

function historyIndex(): number | null {
  const index = window.history.state?.idx;
  return typeof index === "number" ? index : null;
}

export function confirmRouteLeave(scopes?: readonly string[]): boolean {
  if (acceptedActions > 0) return true;
  const applicable = [...guards.values()].filter(
    (guard) => scopes === undefined || guard.scope === undefined || scopes.includes(guard.scope),
  );
  return [...new Set(applicable.map((guard) => guard.confirm))].every((confirm) => confirm());
}

/** Confirm before mutations that destroy editors, including any ensuing synchronous navigation. */
export function runWithLeaveGuards(action: () => void, scopes?: readonly string[]): boolean {
  if (!confirmRouteLeave(scopes)) return false;
  acceptedActions += 1;
  try {
    action();
  } finally {
    acceptedActions -= 1;
  }
  return true;
}

export function rememberRouteNavigation(delta?: number): void {
  const index = historyIndex();
  if (delta !== undefined && index !== null) expectedIndex = index + delta;
  else {
    lastIndex = index;
    expectedIndex = null;
  }
}

function onPopState(event: PopStateEvent): void {
  const next = historyIndex();
  if (next !== null && next === expectedIndex) {
    lastIndex = next;
    expectedIndex = null;
    return;
  }
  expectedIndex = null;
  if (next === null || lastIndex === null || next === lastIndex) return;
  if (confirmRouteLeave()) {
    lastIndex = next;
    return;
  }
  event.stopImmediatePropagation();
  const previous = lastIndex;
  expectedIndex = previous;
  window.history.go(previous - next);
}

export function registerRouteLeaveGuard(guard: () => boolean, scope?: string): () => void {
  if (guards.size === 0) {
    lastIndex = historyIndex();
    window.addEventListener("popstate", onPopState, true);
  }
  const token = Symbol();
  guards.set(token, { confirm: guard, scope });
  return () => {
    guards.delete(token);
    if (guards.size === 0) {
      window.removeEventListener("popstate", onPopState, true);
      lastIndex = null;
      expectedIndex = null;
    }
  };
}
