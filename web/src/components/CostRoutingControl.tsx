import { isNativeTerminalSession } from "@/lib/nativeCodingAgents";
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
 * Native harnesses whose sub-agent spawns go through the native-subagent
 * router, and whose spawn-routing apparatus is installed for any Smart
 * Routing session. Codex is absent on purpose — see
 * {@link isSubagentRoutingSession}.
 */
const CLAUDE_SUBAGENT_ROUTING_HARNESSES: ReadonlySet<string> = new Set(["claude-native"]);

/** Session label recording that Smart Routing owns this session's harness. */
export const AUTO_HARNESS_LABEL_KEY = "omnigent.routing.auto_harness";

/** Whether Smart Routing owns this session's harness (so its spawns may cross families). */
function isAutoHarnessSession(
  session: Pick<Session, "harness" | "labels"> | null | undefined,
): boolean {
  return session?.labels?.[AUTO_HARNESS_LABEL_KEY] === "1" || session?.harness === "auto";
}

/**
 * Whether a session can control the routing of the sub-agents it spawns.
 *
 * Wider than {@link isCostRoutingSession} for non-native sessions: an
 * SDK/bundle agent (Polly, Debby, …) spawns its children through the
 * session-create path, which re-reads this switch per spawn whatever harness
 * the parent runs, so the knob works there regardless of routing state.
 *
 * Narrower for a native terminal, where the whole spawn-routing apparatus is
 * installed at launch and cannot be turned on afterwards:
 *
 * - an auto-harness session always has it;
 * - a Claude-family session has it whenever it launched on Smart Routing;
 * - a native session pinned to Codex never has it (the generated ``hooks.json``
 *   and the routed-spawn tool pre-approvals only come with an auto-harness
 *   launch), and neither does a plain session of any family. A codex-brained
 *   SDK/bundle agent is not this case — it took the non-native branch above,
 *   and the server stamps it to match.
 *
 * Those last two would present "Smart Routing" as choosable while nothing
 * consumed the choice, so the row is hidden instead. Subagent routing is
 * launch-time-fixed for pinned Codex.
 *
 * Callers must also check ``ServerInfo.smart_routing_enabled`` — this
 * predicate only checks the session shape.
 */
export function isSubagentRoutingSession(
  session:
    | Pick<
        Session,
        "agentName" | "parentSessionId" | "harness" | "labels" | "costControlModeOverride"
      >
    | null
    | undefined,
): boolean {
  if (!isCostRoutingSession(session)) return false;
  if (!isNativeTerminalSession(session)) return true;
  if (isAutoHarnessSession(session)) return true;
  return (
    CLAUDE_SUBAGENT_ROUTING_HARNESSES.has(session?.harness ?? "") &&
    session?.costControlModeOverride === "on"
  );
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
