// Stacks folded away in the Pull Requests list, on this device only, per workspace user and
// the account the page reads as. Expanded is the default, so only folds are kept.

import { useCallback, useMemo, useSyncExternalStore } from "react";
import { z } from "zod";

const STORAGE_KEY = "omnigent:pull-requests-collapsed-stacks";
/** The most folds kept across accounts; past it, the oldest is forgotten and shows expanded. */
export const COLLAPSED_STACK_LIMIT = 200;

const savedSchema = z.object({
  version: z.literal(1),
  /** Most recently folded first. */
  stacks: z.array(z.object({ scope: z.string(), url: z.string() })).max(COLLAPSED_STACK_LIMIT * 2),
});

interface CollapsedStack {
  scope: string;
  url: string;
}

const keyOf = (stack: CollapsedStack) => `${stack.scope}\n${stack.url.toLowerCase()}`;

/** `stacks` with `stack` folded, most recent first, within the limit. */
export function withCollapsed(
  stacks: readonly CollapsedStack[],
  stack: CollapsedStack,
): CollapsedStack[] {
  const key = keyOf(stack);
  return [stack, ...stacks.filter((other) => keyOf(other) !== key)].slice(0, COLLAPSED_STACK_LIMIT);
}

export function withoutCollapsed(
  stacks: readonly CollapsedStack[],
  stack: CollapsedStack,
): CollapsedStack[] {
  const key = keyOf(stack);
  return stacks.filter((other) => keyOf(other) !== key);
}

function deviceStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function readCollapsedStacks(): CollapsedStack[] {
  let raw: string | null | undefined;
  try {
    raw = deviceStorage()?.getItem(STORAGE_KEY);
  } catch {
    return [];
  }
  if (!raw) return [];
  try {
    const saved = savedSchema.safeParse(JSON.parse(raw));
    return saved.success ? saved.data.stacks.slice(0, COLLAPSED_STACK_LIMIT) : [];
  } catch {
    return [];
  }
}

const listeners = new Set<() => void>();
// Parsed once per change, so every reader gets the same list between writes.
let cache: CollapsedStack[] | null = null;

function current(): CollapsedStack[] {
  cache ??= readCollapsedStacks();
  return cache;
}

function write(stacks: CollapsedStack[]) {
  try {
    const storage = deviceStorage();
    if (stacks.length > 0) {
      storage?.setItem(STORAGE_KEY, JSON.stringify({ version: 1, stacks }));
    } else {
      storage?.removeItem(STORAGE_KEY);
    }
  } catch {
    // A device that can't save keeps the folds for this run only.
  }
  cache = stacks;
  for (const listener of listeners) listener();
}

let watching = false;

function subscribe(listener: () => void) {
  if (!watching && typeof window !== "undefined") {
    watching = true;
    // Another window on this device folded or unfolded a stack: read the list again.
    window.addEventListener("storage", (event) => {
      if (event.key !== null && event.key !== STORAGE_KEY) return;
      cache = null;
      for (const notify of listeners) notify();
    });
  }
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Test seam: forget the parsed list, so the next read sees storage. */
export function resetCollapsedStacks(): void {
  cache = null;
}

export interface CollapsedStacks {
  isCollapsed: (url: string) => boolean;
  setCollapsed: (url: string, collapsed: boolean) => void;
}

/** The stacks `scope` folded, and how to fold or unfold one; nothing folds while it's null. */
export function useCollapsedStacks(scope: string | null): CollapsedStacks {
  const stacks = useSyncExternalStore(subscribe, current);
  const isCollapsed = useMemo(() => {
    if (scope === null) return () => false;
    const keys = new Set(stacks.filter((stack) => stack.scope === scope).map(keyOf));
    return (url: string) => keys.has(keyOf({ scope, url }));
  }, [scope, stacks]);
  const setCollapsed = useCallback(
    (url: string, collapsed: boolean) => {
      if (scope === null) return;
      const stack = { scope, url };
      write(collapsed ? withCollapsed(current(), stack) : withoutCollapsed(current(), stack));
    },
    [scope],
  );
  return useMemo(() => ({ isCollapsed, setCollapsed }), [isCollapsed, setCollapsed]);
}
