// Why top-level Smart Routing can (or can't) be offered on the new-chat
// landing, and how to say so.
//
// The landing drops a Smart Routing pick it can't honour. Four different
// conditions cause that, and they need different words: a server with routing
// switched off is not a host missing a CLI, neither is a deployment whose
// native wrapper agents aren't registered, and neither is a host whose CLI runs
// off something other than the workspace AI gateway.

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
  | { kind: "harnesses-unready"; harnesses: string[] }
  /**
   * Arms whose family the host doesn't back with the workspace AI gateway. The
   * external router's apply layer rewrites the model through the gateway, so a
   * CLI pointed anywhere else can't be routed by it even with the CLI
   * installed. Only fires when the built-in judge can't answer instead.
   */
  | { kind: "not-gateway-backed"; harnesses: string[] };

/** The routers that can answer a Smart Routing pick. */
export type RouterSource = "databricks-aigw" | "oss-llm";

/**
 * Which router would answer a pick for one harness family — the browser-side
 * mirror of the server's ``select_router`` seam.
 *
 * The external AI-Gateway router can only answer for a family the host runs
 * through the gateway, because the apply layer rewrites the model there. The
 * built-in judge has no such requirement, so it covers the off-gateway arms.
 *
 * @param inputs - The server's two configured sources plus whether the host
 *   backs this family's inference with the workspace AI gateway.
 * @returns The router that answers, or ``null`` when neither can.
 */
export function smartRoutingSourceFor(inputs: {
  externalConfigured: boolean;
  ossConfigured: boolean;
  gatewayBacked: boolean;
}): RouterSource | null {
  if (inputs.externalConfigured && inputs.gatewayBacked) return "databricks-aigw";
  if (inputs.ossConfigured) return "oss-llm";
  return null;
}

/**
 * Classify why Smart Routing can't be offered, most-fundamental cause first —
 * routing being off makes the host's CLIs irrelevant, and a CLI that isn't
 * installed makes its inference config irrelevant.
 *
 * An off-gateway arm is only a loss when the external router is the only one
 * that can answer: with the built-in judge available it routes that arm
 * instead, so ``not-gateway-backed`` stops firing.
 *
 * @param inputs - The independent conditions, read off the server flags,
 *   the agent list, and the selected host.
 * @returns The cause, or ``null`` when Smart Routing is available.
 */
export function smartRoutingUnavailableReason(inputs: {
  routingEnabled: boolean;
  wrappersRegistered: boolean;
  unreadyHarnesses: readonly string[];
  notGatewayBackedHarnesses?: readonly string[];
  ossRoutingAvailable?: boolean;
}): SmartRoutingUnavailableCause | null {
  if (!inputs.routingEnabled) return { kind: "routing-disabled" };
  if (!inputs.wrappersRegistered) return { kind: "wrappers-missing" };
  if (inputs.unreadyHarnesses.length > 0) {
    return { kind: "harnesses-unready", harnesses: [...inputs.unreadyHarnesses] };
  }
  const notBacked = inputs.notGatewayBackedHarnesses ?? [];
  if (notBacked.length > 0 && inputs.ossRoutingAvailable !== true) {
    return { kind: "not-gateway-backed", harnesses: [...notBacked] };
  }
  return null;
}

/**
 * Whether *host* backs *harness*'s inference with the workspace AI gateway.
 *
 * Unknown reads as backed: an older host build (or a server that predates the
 * field) reports nothing, and a sandbox has no host row at all. Gating those
 * away would hide Smart Routing on every deployment that can't yet answer —
 * only an explicit ``false`` from the host is a reason to withhold it.
 */
export function hostBacksHarnessWithGateway(
  host: { gateway_inference?: Record<string, boolean> | null } | null | undefined,
  harness: string,
): boolean {
  return host?.gateway_inference?.[harness] !== false;
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
    case "not-gateway-backed":
      return `${SMART_ROUTING_LABEL} needs ${armList(cause.harnesses)} running on the workspace AI gateway${on} — switched to ${to}.`;
  }
}
