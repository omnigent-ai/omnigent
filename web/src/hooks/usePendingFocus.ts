// A focus move that waits for its target to appear, such as a list still loading.
// Any key or pointer press, or focus leaving `scope`, drops it, so a late target
// can't take focus from wherever the person went in the meantime.

import { type RefObject, useCallback, useEffect, useState } from "react";

/** One request; a new object each time, so asking for the same target again still moves focus. */
export interface PendingFocus<Target> {
  target: Target;
}

export function usePendingFocus<Target>(scope: RefObject<HTMLElement | null>) {
  const [pending, setPending] = useState<PendingFocus<Target> | null>(null);
  const request = useCallback((target: Target) => setPending({ target }), []);
  /** The target took focus, or the move no longer applies. */
  const settle = useCallback(() => setPending(null), []);

  useEffect(() => {
    if (!pending) return;
    const drop = () => setPending(null);
    const leave = (event: FocusEvent) => {
      const element = scope.current;
      if (element && event.target instanceof Node && !element.contains(event.target)) drop();
    };
    document.addEventListener("keydown", drop, true);
    document.addEventListener("pointerdown", drop, true);
    document.addEventListener("focusin", leave, true);
    return () => {
      document.removeEventListener("keydown", drop, true);
      document.removeEventListener("pointerdown", drop, true);
      document.removeEventListener("focusin", leave, true);
    };
  }, [pending, scope]);

  return { pending, request, settle };
}
