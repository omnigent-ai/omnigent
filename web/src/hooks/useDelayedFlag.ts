import { useEffect, useState } from "react";

/** How long a load runs before its loading indicator appears. */
export const LOADING_INDICATOR_DELAY_MS = 250;

/**
 * Whether `active` has stayed true for `delayMs`. Loading indicators use it so
 * answers that arrive quickly never flash one; it resets as soon as `active`
 * turns false, or when `restartKey` changes, as a retry of a still-pending read does.
 */
export function useDelayedFlag(
  active: boolean,
  delayMs = LOADING_INDICATOR_DELAY_MS,
  restartKey?: unknown,
): boolean {
  const [elapsed, setElapsed] = useState(false);
  useEffect(() => {
    setElapsed(false);
    if (!active) return;
    const timer = setTimeout(() => setElapsed(true), delayMs);
    return () => clearTimeout(timer);
  }, [active, delayMs, restartKey]);
  return active && elapsed;
}
