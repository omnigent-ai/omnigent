import { useEffect, useState } from "react";

/** How long a load runs before its loading indicator appears. */
export const LOADING_INDICATOR_DELAY_MS = 250;

/**
 * Whether `active` has stayed true for `delayMs`. Loading indicators use it so
 * answers that arrive quickly never flash one; it resets as soon as `active` turns false.
 */
export function useDelayedFlag(active: boolean, delayMs = LOADING_INDICATOR_DELAY_MS): boolean {
  const [elapsed, setElapsed] = useState(false);
  useEffect(() => {
    setElapsed(false);
    if (!active) return;
    const timer = setTimeout(() => setElapsed(true), delayMs);
    return () => clearTimeout(timer);
  }, [active, delayMs]);
  return active && elapsed;
}
