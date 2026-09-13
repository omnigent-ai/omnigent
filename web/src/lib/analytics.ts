/**
 * Product-analytics abstraction.
 *
 * Inversion of control: the app emits high-level events (clicks, field
 * value-changes, page views) and the *host* decides what to do with them via
 * `OmnigentHostConfig.analytics` (see `lib/host.ts`). When no host sink is
 * configured — standalone web, or before the host wires it — every emit is a
 * no-op, so the app records nothing on its own. This mirrors the `searchUsers`
 * IoC pattern.
 *
 * Distinct from `lib/telemetry.ts`, which is low-level OpenTelemetry HTTP
 * tracing; this module is user-action analytics keyed by a stable componentId.
 *
 * PII: field values are redacted by default. A value is only forwarded when the
 * call site explicitly declares it PII-free (`{ valueHasNoPii: true }`), and
 * even then free-form strings should be avoided — prefer enumerable values.
 */

import { useEffect, useRef } from "react";
import { useLocation } from "@/lib/routing";

// The routing-free emit primitives live in `lib/analyticsEmit.ts` (so
// `lib/routing.tsx` can emit without a routing↔analytics import cycle). Re-export
// them here so this module stays the single import surface for analytics.
export {
  emitOmnigentAnalytics,
  emitInteractionPhase,
  useOmnigentAnalytics,
  type OmnigentAnalytics,
  type TrackValueChangeOptions,
  type InteractionPhaseArgs,
} from "@/lib/analyticsEmit";
import { emitOmnigentAnalytics } from "@/lib/analyticsEmit";

/**
 * Report a page view for the given stable `pageId`. Each page calls this at the
 * top of its body with its own explicit id (e.g. `useOmnigentPageView("chat")`)
 * — the page is the source of truth for its id, so there's no central route→id
 * table.
 *
 * Re-fires whenever the URL pathname changes, even if `pageId` is unchanged —
 * matching the Databricks unified router, where a same-page param change (chat
 * `/c/a` → `/c/b`) is still a distinct page view. So a component that stays
 * mounted across such a navigation (ChatPage does) still emits one page view per
 * destination, all under the same `pageId`.
 */
export function useOmnigentPageView(pageId: string): void {
  const { pathname } = useLocation();
  const lastFired = useRef<string | null>(null);
  useEffect(() => {
    // Key on pageId + pathname so a mounted page re-emits on a param change but
    // not on unrelated re-renders.
    const key = `${pageId} ${pathname}`;
    if (lastFired.current === key) return;
    lastFired.current = key;
    emitOmnigentAnalytics({ type: "page_view", pageId });
  }, [pageId, pathname]);
}
