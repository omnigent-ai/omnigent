/**
 * Routing-free analytics emit primitives.
 *
 * Split out from `lib/analytics.ts` so modules that only need to *emit* an event
 * (e.g. `lib/routing.tsx`'s Link) can import these without pulling in
 * `useOmnigentPageView`, which imports `useLocation` from `lib/routing` — that
 * would form a routing↔analytics import cycle. The page-view hook stays in
 * `lib/analytics.ts`; both are re-exported there so existing call sites are
 * unchanged.
 *
 * See `lib/analytics.ts` for the IoC rationale and PII policy.
 */

import { useMemo } from "react";
import {
  getOmnigentAnalytics,
  type OmnigentAnalyticsEvent,
  type OmnigentComponentKind,
} from "@/lib/host";

/**
 * Emit one analytics event to the host sink. No-op when no host is configured.
 * Safe to call from anywhere (event handlers, effects) — it is not a hook.
 */
export function emitOmnigentAnalytics(event: OmnigentAnalyticsEvent): void {
  // Wrappers emit before the caller's handler runs, so a throwing sink must not
  // suppress the user's action.
  try {
    getOmnigentAnalytics()?.(event);
  } catch {
    // ignore
  }
}

export interface TrackValueChangeOptions {
  /**
   * Set true ONLY when the value is known non-PII (e.g. a selection from a
   * fixed set, a boolean toggle, a count). When false/omitted the value is
   * dropped and only the fact that the field changed is reported.
   */
  valueHasNoPii?: boolean;
}

export interface OmnigentAnalytics {
  trackClick: (componentId: string, componentKind?: OmnigentComponentKind) => void;
  trackValueChange: (
    componentId: string,
    componentKind?: OmnigentComponentKind,
    value?: string | number | boolean,
    options?: TrackValueChangeOptions,
  ) => void;
}

/**
 * Stable analytics callbacks for use in components. The returned object is
 * referentially stable for the lifetime of the component (the sink is read
 * lazily inside each call), so it's safe in effect/callback deps.
 */
export function useOmnigentAnalytics(): OmnigentAnalytics {
  return useMemo<OmnigentAnalytics>(
    () => ({
      trackClick: (componentId, componentKind) =>
        emitOmnigentAnalytics({ type: "click", componentId, componentKind }),
      trackValueChange: (componentId, componentKind, value, options) =>
        emitOmnigentAnalytics({
          type: "value_change",
          componentId,
          componentKind,
          // Redact by default: only forward the value when the caller vouches
          // it carries no PII.
          value: options?.valueHasNoPii ? value : undefined,
        }),
    }),
    [],
  );
}
