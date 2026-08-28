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
  type OmnigentInteractionKind,
  type OmnigentInteractionStatus,
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

export interface InteractionPhaseArgs {
  /** Correlates the `start` and `complete` of one interaction. */
  interactionId: string;
  interactionKind: OmnigentInteractionKind;
  phase: "start" | "complete";
  /** Set on `complete`. */
  status?: OmnigentInteractionStatus;
  /** Optional bounded, non-PII label (e.g. a tool name). Never user content. */
  name?: string;
  /** Elapsed time, set on `complete` when known. */
  durationMs?: number;
}

/**
 * Emit an interaction-phase event (agent run / tool call / approval, start or
 * completion). A routing-free free function so non-React call sites — the event
 * reducer in `lib/blockStream.ts`, store thunks in `store/chatStore.ts` — can
 * report outcomes. No-op when no host sink is configured.
 */
export function emitInteractionPhase(args: InteractionPhaseArgs): void {
  emitOmnigentAnalytics({ type: "interaction_phase", ...args });
}

// A random correlation id for a timed interaction that has no natural subject id
// (creating a session, loading the list). Guarded so a missing `crypto.randomUUID`
// can never throw into the wrapped operation.
function newInteractionId(): string {
  try {
    if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  } catch {
    // fall through to the timestamp fallback
  }
  return `iid-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Handle for an in-flight timed interaction opened by `startTimedInteraction`. */
export interface TimedInteraction {
  /**
   * Report the terminal outcome and elapsed `durationMs` (defaults to
   * "success"; pass `null` to omit status entirely — for interactions with no
   * reliable outcome signal, such as tool calls). Idempotent: only the first
   * settle emits, so a double-settle (e.g. a catch after a partial success)
   * can never double-count.
   */
  complete: (status?: OmnigentInteractionStatus | null) => void;
  /** Shorthand for `complete("failure")`. */
  fail: () => void;
}

/**
 * Begin a timed interaction: emit its `start` phase now and return a handle
 * whose `complete()` / `fail()` emits the `complete` phase with the elapsed
 * `durationMs`. Detached — `start` and the terminal call may live in different
 * scopes, and the caller keeps control of its own operation (the handle never
 * wraps or touches it). Emitting never throws (`emitInteractionPhase` no-ops
 * with no host sink and swallows sink errors). Pass an `interactionId` for a
 * natural subject (e.g. a session id); omit it for a generated correlation id.
 *
 *   const interaction = startTimedInteraction("get_session", sessionId);
 *   try { await load(); interaction.complete(); } catch (e) { interaction.fail(); throw e; }
 *
 * `name` is an optional bounded, non-PII label (e.g. a tool name) carried on both
 * phases; never user content.
 */
export function startTimedInteraction(
  interactionKind: OmnigentInteractionKind,
  interactionId: string = newInteractionId(),
  name?: string,
): TimedInteraction {
  const startedAt = Date.now();
  let settled = false;
  const label = name ? { name } : {};
  emitInteractionPhase({ interactionId, interactionKind, phase: "start", ...label });
  const complete = (status: OmnigentInteractionStatus | null = "success"): void => {
    if (settled) return;
    settled = true;
    emitInteractionPhase({
      interactionId,
      interactionKind,
      phase: "complete",
      // `null` omits status: an interaction with no reliable outcome signal
      // (a tool call) must not report a fabricated success/failure.
      ...(status !== null ? { status } : {}),
      ...label,
      durationMs: Date.now() - startedAt,
    });
  };
  return { complete, fail: () => complete("failure") };
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
  /** Report an interaction-phase event (agent run / tool call / approval). */
  trackInteraction: (args: InteractionPhaseArgs) => void;
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
      trackInteraction: (args) => emitInteractionPhase(args),
    }),
    [],
  );
}
