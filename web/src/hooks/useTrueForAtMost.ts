import { useEffect, useState } from "react";

/**
 * Follow `active`, but drop to `false` once it has stayed `true` for `maxMs`.
 * The window restarts each time `active` turns `true` or `resetKey` changes.
 */
export function useTrueForAtMost(
  active: boolean,
  maxMs: number,
  resetKey: unknown = null,
): boolean {
  const [span, setSpan] = useState({ active, resetKey, expired: false });
  if (span.active !== active || !Object.is(span.resetKey, resetKey)) {
    // A new window starts unexpired in this same render.
    setSpan({ active, resetKey, expired: false });
  }
  useEffect(() => {
    if (!active) return undefined;
    const timer = window.setTimeout(
      () => setSpan((current) => ({ ...current, expired: true })),
      maxMs,
    );
    return () => window.clearTimeout(timer);
  }, [active, maxMs, resetKey]);
  const current = span.active === active && Object.is(span.resetKey, resetKey);
  return active && !(current && span.expired);
}
