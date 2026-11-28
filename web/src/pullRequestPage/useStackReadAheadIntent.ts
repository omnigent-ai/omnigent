// The user's interest in a stack member, from its row in the stack menu or the Overview's card:
// a pointer or focus resting on it, or a press, moves that member's reads ahead until it ends.

import { useCallback, useEffect, useMemo, useRef } from "react";

import type { StackReadAhead } from "@/lib/pullRequestStacks/stackWarmup";

/** How long the pointer or focus must rest on a row before it counts as interest. */
export const STACK_READ_AHEAD_INTENT_DELAY_MS = 100;

/** Event handlers to spread onto a stack member's row. */
export interface StackReadAheadIntentHandlers {
  onPointerEnter: () => void;
  onPointerLeave: () => void;
  onFocus: () => void;
  onBlur: () => void;
  onPointerDown: () => void;
}

/**
 * Handlers that move `url`'s reads ahead while the pointer or focus rests on its row. Sweeping
 * across rows sends nothing; leaving the row, losing focus, unmounting or another member ends
 * it, so a member the user moved past keeps its usual place in the queue.
 *
 * @param readAhead The stack's read-ahead, absent where nothing reads it ahead.
 * @param url The row's member, or `null` for a row that opens nothing here.
 */
export function useStackReadAheadIntent(
  readAhead: StackReadAhead | undefined,
  url: string | null,
): StackReadAheadIntentHandlers {
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const endInterest = useRef<(() => void) | null>(null);
  // Hover and focus each hold the interest; it ends once neither does.
  const holders = useRef({ pointer: false, focus: false });

  const end = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
    endInterest.current?.();
    endInterest.current = null;
  }, []);

  const begin = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
    if (endInterest.current || !readAhead || url === null) return;
    endInterest.current = readAhead(url);
  }, [readAhead, url]);

  // A row that unmounts, as the menu closes, or now shows another member, lets its interest go.
  useEffect(() => {
    const held = holders.current;
    return () => {
      held.pointer = false;
      held.focus = false;
      end();
    };
  }, [readAhead, url, end]);

  const hold = useCallback(
    (source: "pointer" | "focus") => {
      holders.current[source] = true;
      if (endInterest.current || timer.current !== null || !readAhead || url === null) return;
      timer.current = setTimeout(begin, STACK_READ_AHEAD_INTENT_DELAY_MS);
    },
    [begin, readAhead, url],
  );

  const letGo = useCallback(
    (source: "pointer" | "focus") => {
      holders.current[source] = false;
      if (!holders.current.pointer && !holders.current.focus) end();
    },
    [end],
  );

  return useMemo(
    () => ({
      onPointerEnter: () => hold("pointer"),
      onPointerLeave: () => letGo("pointer"),
      onFocus: () => hold("focus"),
      onBlur: () => letGo("focus"),
      onPointerDown: begin,
    }),
    [hold, letGo, begin],
  );
}
