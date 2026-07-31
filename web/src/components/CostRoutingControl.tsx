import type { Session } from "@/lib/types";

/** Per-session cost-control switch value; `null` = unset (presents as off). */
export type CostControlMode = "on" | "off" | null;

/**
 * Whether a session is eligible for smart routing (top-level, has agent).
 *
 * Callers must also check ``ServerInfo.smart_routing_enabled`` from
 * the ``/v1/info`` probe to decide whether to show the toggle — this
 * predicate only checks the session shape.
 */
export function isCostRoutingSession(
  session: Pick<Session, "agentName" | "parentSessionId"> | null | undefined,
): boolean {
  return session?.agentName != null && session.parentSessionId == null;
}

/**
 * Harnesses whose sub-agent spawns go through the native-subagent router:
 * Claude Code and Codex in both flavours, plus the ``"auto"`` sentinel a
 * fully-auto session carries until its first message resolves a real harness.
 */
const SUBAGENT_ROUTING_HARNESSES: ReadonlySet<string> = new Set([
  "claude-native",
  "claude-sdk",
  "codex",
  "codex-native",
  "auto",
]);

/**
 * Whether a session can control the routing of the sub-agents it spawns.
 *
 * Unlike {@link isCostRoutingSession} this holds for native-terminal Claude /
 * Codex sessions too: the native CLI bakes its OWN model at launch, but the
 * sub-agents it spawns are routed per spawn, so the knob is meaningful there.
 *
 * Callers must also check ``ServerInfo.smart_routing_enabled`` — this
 * predicate only checks the session shape.
 */
export function isSubagentRoutingSession(
  session: Pick<Session, "agentName" | "parentSessionId" | "harness"> | null | undefined,
): boolean {
  if (!isCostRoutingSession(session)) return false;
  return SUBAGENT_ROUTING_HARNESSES.has(session?.harness ?? "");
}

// The tier-defining token of Claude model ids ("databricks-claude-haiku-4-5" → "haiku").
const MODEL_FAMILY_HINTS = ["haiku", "sonnet", "opus"] as const;

/**
 * Friendly short name for a model id, for the routing decision chip and the
 * SmartRoutingCard plan rows.
 *
 * Lossy is fine — these are glance surfaces, not an audit log.
 *
 * @param model Model id, e.g. `"databricks-claude-haiku-4-5"`.
 * @returns The short display name, e.g. `"haiku"`.
 */
export function shortModelName(model: string): string {
  const lower = model.toLowerCase();
  for (const family of MODEL_FAMILY_HINTS) {
    if (lower.includes(family)) return family;
  }
  return lower.startsWith("databricks-") ? model.slice("databricks-".length) : model;
}
