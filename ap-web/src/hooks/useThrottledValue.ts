// Trailing-edge throttle for a value that can change on every render. Used to
// bound how often the live (growing) assistant bubble re-parses its markdown:
// the rAF-batched store pump can commit a new, longer text on every frame, and
// each commit otherwise forces Streamdown to re-parse the whole accumulated
// message (O(message length) per frame). Throttling caps that re-parse to a few
// times per second while still converging to the final text.

import { useEffect, useRef, useState } from "react";

/**
 * Throttle a frequently-changing value to at most one update per interval,
 * trailing-edge.
 *
 * The initial value is returned as-is on mount. When the value changes, the
 * change is emitted immediately if at least ``intervalMs`` has elapsed since the
 * last emit (or nothing has been emitted yet); otherwise a single trailing flush
 * is scheduled at the interval boundary so the latest value is never dropped.
 * This keeps a per-frame stream of updates (e.g. token-by-token markdown growth)
 * from forcing heavy work on every frame while still settling on the final value
 * within ``intervalMs`` of the last change.
 *
 * :param value: The latest value; may change on every render. In practice a
 *     markdown string, e.g. ``"## Heading\n\nbody…"``.
 * :param intervalMs: Minimum gap between emitted updates, e.g. ``100``.
 * :returns: The throttled value, lagging ``value`` by at most ``intervalMs``.
 */
export function useThrottledValue<T>(value: T, intervalMs: number): T {
  const [shown, setShown] = useState<T>(value);
  // Date.now() of the last emit; 0 means "nothing emitted since mount" so the
  // first change emits immediately even if the test clock starts at 0.
  const lastEmitRef = useRef(0);
  const timerRef = useRef<number>(0);
  // Latest value, read lazily by the trailing flush so it emits the newest.
  const latestRef = useRef<T>(value);
  latestRef.current = value;

  useEffect(() => {
    if (Object.is(shown, value)) return;
    const elapsed = Date.now() - lastEmitRef.current;
    if (lastEmitRef.current === 0 || elapsed >= intervalMs) {
      // Past the interval (or first emit): show the new value now.
      window.clearTimeout(timerRef.current);
      timerRef.current = 0;
      lastEmitRef.current = Date.now();
      setShown(value);
      return;
    }
    // Within the interval: schedule one trailing flush; don't reschedule if a
    // flush is already pending (it reads the newest value via latestRef).
    if (timerRef.current === 0) {
      timerRef.current = window.setTimeout(() => {
        timerRef.current = 0;
        lastEmitRef.current = Date.now();
        setShown(latestRef.current);
      }, intervalMs - elapsed);
    }
  }, [value, intervalMs, shown]);

  // Drop any pending flush on unmount; never setState after teardown.
  useEffect(
    () => () => {
      window.clearTimeout(timerRef.current);
    },
    [],
  );

  return shown;
}
