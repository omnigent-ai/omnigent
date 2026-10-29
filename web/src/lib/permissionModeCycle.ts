// Shift+Tab steps through every option in the permission chip's menu order.

import { isImeCompositionKeyEvent } from "@/lib/ime";

/** The key fields the cycle check reads from a React keyboard event. */
export interface PermissionModeCycleKeyEvent {
  key: string;
  shiftKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  nativeEvent: { isComposing?: boolean; keyCode?: number };
}

/** Bare Shift+Tab (no Ctrl, Cmd or Alt), outside IME composition. */
export function isPermissionModeCycleKey(event: PermissionModeCycleKeyEvent): boolean {
  return (
    event.key === "Tab" &&
    event.shiftKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey &&
    !isImeCompositionKeyEvent(event)
  );
}

/**
 * Next option after *current* in menu order, wrapping; an unknown
 * *current* starts from the top. Skips *unavailable*. Null when none is reachable.
 */
export function nextCycledPermissionMode(
  options: readonly { value: string }[],
  current: string | null | undefined,
  unavailable: ReadonlySet<string> = new Set(),
): string | null {
  const start = options.findIndex((option) => option.value === current);
  for (let step = 1; step <= options.length; step += 1) {
    const candidate = options[(start + step) % options.length]!.value;
    if (candidate === current) continue;
    if (unavailable.has(candidate)) continue;
    return candidate;
  }
  return null;
}
