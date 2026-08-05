// Shared shape for the routing-identity fields that ride alongside a routing
// decision's `model`/`applied`/`rationale`/`agent`.
//
// Every hop of the transcript pipeline (SSE event → block → bubble →
// chip/card) carries this shape through unchanged under a single `routing`
// field. All fields are optional; legacy rows carry none and render as before.

/** Where a routing decision was taken. Optional; legacy rows omit it. */
export type RoutingScope = "session" | "turn" | "child_session" | "native_subagent";

export interface RoutingDecisionExtras {
  /** Harness the decision routes to, e.g. `"claude-native"`. */
  harness?: string | null;
  /** Decision scope, e.g. `"native_subagent"` for an in-harness Task spawn. */
  scope?: RoutingScope | null;
  /** Server-side decision identity, cross-referenced by routing telemetry. */
  decisionId?: string | null;
  /** Router-vocabulary pick before catalog resolution, e.g. `"gpt-5-6-sol"`. */
  rawModel?: string | null;
  /** Model the spawn asked for and the router overrode, when there was one. */
  attemptedOverride?: string | null;
  /** Which router answered — `"databricks-aigw"` or `"oss-llm"`; absent on legacy rows. */
  routerSource?: string | null;
}

const SCOPES = new Set<string>(["session", "turn", "child_session", "native_subagent"]);

/** Scopes whose decision belongs to a sub-agent rather than the session itself. */
const SUBAGENT_SCOPES = new Set<RoutingScope>(["child_session", "native_subagent"]);

function str(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

/**
 * Read the routing-identity fields off a snake_case wire record (an SSE
 * `routing_decision` payload or a stored transcript item).
 *
 * Unknown/blank values are dropped rather than coerced, so a partial or
 * older payload yields an empty object and the UI falls back to the
 * original four-field rendering.
 *
 * @param rec - Raw wire record, e.g. `{model: "…", raw_model: "gpt-5-6-sol"}`.
 * @returns Only the fields actually present.
 */
export function routingExtrasFromWire(rec: Record<string, unknown>): RoutingDecisionExtras {
  const scope = str(rec.scope);
  return {
    ...(str(rec.harness) !== undefined && { harness: str(rec.harness) }),
    ...(scope !== undefined && SCOPES.has(scope) && { scope: scope as RoutingScope }),
    ...(str(rec.decision_id) !== undefined && { decisionId: str(rec.decision_id) }),
    ...(str(rec.raw_model) !== undefined && { rawModel: str(rec.raw_model) }),
    ...(str(rec.attempted_override) !== undefined && {
      attemptedOverride: str(rec.attempted_override),
    }),
    ...(str(rec.router_source) !== undefined && { routerSource: str(rec.router_source) }),
  };
}

/**
 * Re-copy already-parsed extras for the camelCase hops (event → block →
 * bubble). Unset fields stay unset, so the result never carries `undefined`.
 *
 * @param source - The carrier's `routing` field, or null/undefined when absent.
 * @returns Only the fields actually present.
 */
export function routingExtras(
  source: RoutingDecisionExtras | null | undefined,
): RoutingDecisionExtras {
  if (source == null) return {};
  return {
    ...(source.harness != null && { harness: source.harness }),
    ...(source.scope != null && { scope: source.scope }),
    ...(source.decisionId != null && { decisionId: source.decisionId }),
    ...(source.rawModel != null && { rawModel: source.rawModel }),
    ...(source.attemptedOverride != null && { attemptedOverride: source.attemptedOverride }),
    ...(source.routerSource != null && { routerSource: source.routerSource }),
  };
}

/**
 * Whether a decision belongs to the session's own turn rather than to a
 * sub-agent it spawned. Rows with no scope count as the session's.
 *
 * @param scope - Decision scope, or null/undefined on legacy rows.
 * @returns True for `session`/`turn`/absent, false for the sub-agent scopes.
 */
export function isSessionScopedDecision(scope: RoutingScope | null | undefined): boolean {
  return scope == null || !SUBAGENT_SCOPES.has(scope);
}

/**
 * Whether a routing-decision chip may render, given the session's
 * ``subagent_routing_override``.
 *
 * An in-harness spawn decision (`native_subagent`) shows only while the
 * session's two-state switch is ``"on"``. That is now an exact mirror of
 * behavior rather than a display policy: an unstamped session's spawns
 * genuinely are not routed, so there is no per-spawn pick to advertise.
 * Historic rows stay persisted as the audit trail either way.
 *
 * Session/turn decisions and child-session decisions are unaffected: those
 * follow the session's own Smart Routing switch, not this override.
 *
 * @param scope - Decision scope, or null/undefined on legacy rows.
 * @param subagentRoutingOverride - The session's stored override.
 * @returns True when the chip should render.
 */
export function showsRoutingDecisionChip(
  scope: RoutingScope | null | undefined,
  subagentRoutingOverride: "on" | "off" | null | undefined,
): boolean {
  return scope !== "native_subagent" || subagentRoutingOverride === "on";
}

/**
 * Badge text for a sub-agent-scoped decision, e.g. `"subagent: researcher"`.
 *
 * @param scope - Decision scope; only the sub-agent scopes get a badge.
 * @param agent - Sub-agent name carried on the decision, when known.
 * @returns The badge text, or `null` for session/turn decisions.
 */
export function subagentScopeLabel(
  scope: RoutingScope | null | undefined,
  agent: string | null | undefined,
): string | null {
  if (scope == null || !SUBAGENT_SCOPES.has(scope)) return null;
  const name = agent?.trim();
  return name ? `subagent: ${name}` : "subagent";
}
