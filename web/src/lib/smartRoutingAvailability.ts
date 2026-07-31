// Why top-level Smart Routing can (or can't) be offered on the new-chat
// landing, and how to say so.
//
// The landing drops a Smart Routing pick it can't honour. Three different
// conditions cause that, and they need different words: a server with routing
// switched off is not a host missing a CLI, and neither is a deployment whose
// native wrapper agents aren't registered.

import { SMART_ROUTING_LABEL } from "@/lib/agentLabels";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";

/**
 * The two native harnesses Smart Routing picks between. Both wrapper agents
 * must be registered and both CLIs ready — a router with one arm is just that
 * arm.
 */
export const SMART_ROUTING_ARMS = ["claude-native", "codex-native"] as const;

/** Why Smart Routing is unavailable, in the shape the notice needs. */
export type SmartRoutingUnavailableCause =
  /** The server has routing switched off (`smart_routing_enabled: false`). */
  | { kind: "routing-disabled" }
  /** A native wrapper agent Smart Routing binds isn't registered. */
  | { kind: "wrappers-missing" }
  /** Ready-to-run arms are missing on the selected host. */
  | { kind: "harnesses-unready"; harnesses: string[] };

/**
 * Classify why Smart Routing can't be offered, most-fundamental cause first —
 * routing being off makes the host's CLIs irrelevant.
 *
 * @param inputs - The three independent conditions, read off the server flags,
 *   the agent list, and the selected host.
 * @returns The cause, or ``null`` when Smart Routing is available.
 */
export function smartRoutingUnavailableReason(inputs: {
  routingEnabled: boolean;
  wrappersRegistered: boolean;
  unreadyHarnesses: readonly string[];
}): SmartRoutingUnavailableCause | null {
  if (!inputs.routingEnabled) return { kind: "routing-disabled" };
  if (!inputs.wrappersRegistered) return { kind: "wrappers-missing" };
  if (inputs.unreadyHarnesses.length > 0) {
    return { kind: "harnesses-unready", harnesses: [...inputs.unreadyHarnesses] };
  }
  return null;
}

/** Display name for an arm, e.g. ``"Codex"``; the raw id if it isn't native. */
function armLabel(harness: string): string {
  return nativeCodingAgentForHarness(harness)?.displayName ?? harness;
}

/** ``"Claude Code and Codex"`` — the arms, in wire order. */
function armList(harnesses: readonly string[]): string {
  const labels = harnesses.map(armLabel);
  if (labels.length === 0) return SMART_ROUTING_ARMS.map(armLabel).join(" and ");
  if (labels.length === 1) return labels[0]!;
  return `${labels.slice(0, -1).join(", ")} and ${labels.at(-1)}`;
}

/**
 * One sentence for the landing's downgrade notice: what took Smart Routing
 * away, and what runs instead.
 *
 * @param cause - From {@link smartRoutingUnavailableReason}.
 * @param context - Host the arms were checked on (omitted for a sandbox) and
 *   the agent the pick fell back to.
 */
export function smartRoutingDroppedMessage(
  cause: SmartRoutingUnavailableCause,
  context: { hostName?: string | null; fallbackAgentName?: string | null } = {},
): string {
  const on = context.hostName ? ` on ${context.hostName}` : "";
  const to = context.fallbackAgentName ?? "the default agent";
  switch (cause.kind) {
    case "routing-disabled":
      return `${SMART_ROUTING_LABEL} is turned off on this server — switched to ${to}.`;
    case "wrappers-missing":
      return `${SMART_ROUTING_LABEL} needs the ${armList(SMART_ROUTING_ARMS)} agents registered on this server — switched to ${to}.`;
    case "harnesses-unready":
      return `${SMART_ROUTING_LABEL} needs ${armList(cause.harnesses)} ready${on} — switched to ${to}.`;
  }
}
