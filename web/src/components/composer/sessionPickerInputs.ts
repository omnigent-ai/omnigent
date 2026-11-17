import { useMemo } from "react";
import { agentDisplayLabel } from "@/components/AgentInfo";
import { isCostRoutingSession } from "@/components/CostRoutingControl";
import { useConversationEntryValue } from "@/hooks/useConversationEntryState";
import { useHostModelOptions, useHosts } from "@/hooks/useHosts";
import { BRAIN_HARNESS_LABELS, useHarnessEffortFamilies } from "@/lib/agentLabels";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import {
  effortLevelsForConv,
  modelPickerKindForConv,
  sdkEffortFamilyForHarness,
  shouldShowEffortPicker,
  type NativeModelPickerKind,
} from "@/lib/composerCapabilities";
import {
  isNativeTerminalSession,
  nativeCodingAgentForSession,
  nativeCodingAgentForSubagentWrapper,
} from "@/lib/nativeCodingAgents";
import {
  SMART_ROUTING_ARMS,
  hostBacksHarnessWithGateway,
  smartRoutingSourceFor,
} from "@/lib/smartRoutingAvailability";
import type { NativeModelOption, Session } from "@/lib/types";
import { useChatStore, type ConversationState } from "@/store/chatStore";

/** Server-info as consumers see it: the probe's result, or "loading". */
export type ServerInfoValue = ServerInfo | "loading";

/**
 * A field of *sessionId*'s conversation, or of the active conversation when
 * omitted (the main composer's case, read straight from the root store).
 */
export function useSessionField<T>(
  sessionId: string | undefined,
  select: (state: ConversationState) => T,
): T {
  // Each half reads only its own source, so neither re-renders for the other's updates.
  const active = useChatStore((state) => (sessionId === undefined ? select(state) : null));
  const entry = useConversationEntryValue(sessionId ?? null, select);
  return sessionId === undefined ? (active as T) : entry;
}

/**
 * Whether the session's own model can be routed per turn.
 *
 * SDK/bundle agent sessions need only the deployment flag. Native Claude
 * Code / Codex panes ARE routable per turn — the server injects the routed
 * pick via ``/model`` when ``cost_control_mode_override`` is on, the same
 * apparatus the create-time gear arms — but only when a router can answer
 * for their family (the server rejects a routing-on create otherwise): the
 * external AI-Gateway router needs the family's inference gateway-backed on
 * the session's host, and the built-in judge covers the rest. An absent
 * host row reads as backed, mirroring {@link hostBacksHarnessWithGateway}.
 */
export function isCostRoutingEligible(
  serverInfo: ServerInfoValue,
  // Only the fields the guards below read, so a temp/optimistic session can be
  // evaluated from its seed without fabricating a whole Session. A real Session
  // is structurally assignable.
  session: Pick<Session, "agentName" | "parentSessionId" | "harness" | "labels"> | null | undefined,
  host?: { gateway_inference?: Record<string, boolean> | null } | null,
): boolean {
  if (serverInfo === "loading" || !serverInfo.smart_routing_enabled) return false;
  if (!isCostRoutingSession(session)) return false;
  if (!isNativeTerminalSession(session)) return true;
  const native = nativeCodingAgentForSession(session);
  if (native === undefined || !SMART_ROUTING_ARMS.some((arm) => arm === native.harness)) {
    return false;
  }
  return (
    smartRoutingSourceFor({
      externalConfigured: serverInfo.smart_routing_sources.external,
      ossConfigured: serverInfo.smart_routing_sources.oss,
      gatewayBacked: hostBacksHarnessWithGateway(host, native.harness),
    }) !== null
  );
}

/**
 * Identity label for the composer status tray: which harness/agent is
 * running this session. Native vendor wrappers read as the bare vendor
 * name ("Claude" / "Codex"); SDK/bundle agents read as the agent name
 * with the brain harness in parens ("Polly (Pi)"). Lives in the status tray
 * below the composer, separate from the read-only model/effort label.
 *
 * A native sub-agent child (a Claude Code Task, a Codex collab thread) reads
 * as its vendor's product name ("Claude Code"), matching the Agents rail's
 * main row. Its ``sub_agent_name`` is the VENDOR's agent type — Claude's
 * ``subagent_type``, e.g. ``"general-purpose"`` — not an Omnigent agent, so
 * the wrapper label has to win over the name-based path below; the instance
 * itself is already named in the "Chatting with sub-agent …" tray.
 *
 * @param modelPickerKind - Native picker family, when the session is a
 *   claude-/codex-/cursor-native wrapper.
 * @param agentName - Bound agent name (lowercase slug), if any.
 * @param sessionHarness - Effective brain harness id (override-aware).
 * @param harnessLabels - harness id → picker label.
 * @param wrapper - The session's ``omnigent.wrapper`` label, if any.
 * @returns Display label, or ``null`` when nothing is known.
 */
export function composerHarnessLabel(
  modelPickerKind: NativeModelPickerKind | null,
  agentName: string | null | undefined,
  sessionHarness: string | null,
  harnessLabels: Record<string, string> = BRAIN_HARNESS_LABELS,
  wrapper: string | null = null,
): string | null {
  const nativeSubagent = nativeCodingAgentForSubagentWrapper(wrapper);
  if (nativeSubagent) return nativeSubagent.displayName;
  if (modelPickerKind === "claude") return "Claude";
  if (modelPickerKind === "codex") return "Codex";
  if (modelPickerKind === "cursor") return "Cursor";
  if (modelPickerKind === "kiro") return "Kiro";
  if (modelPickerKind === "opencode") return "OpenCode";
  if (modelPickerKind === "devin") return "Devin";
  const display = agentName ? agentDisplayLabel(agentName) : null;
  const harness = sessionHarness ? (harnessLabels[sessionHarness] ?? null) : null;
  if (display && harness) return `${display} (${harness})`;
  return display ?? harness;
}

/** The capabilities a session's own snapshot gives its picker gates; empty until it loads. */
export interface SessionCapabilitySource {
  labels: Record<string, string | null>;
  harness?: string | null;
  parentSessionId?: string | null;
  inferenceConfigured?: boolean;
}

export function capabilitySourceForSession(
  session: Session | null | undefined,
): SessionCapabilitySource {
  if (!session) return { labels: {}, harness: null, parentSessionId: null };
  return {
    labels: session.labels ?? {},
    harness: session.harness,
    inferenceConfigured: session.inferenceConfigured,
    parentSessionId: session.parentSessionId ?? null,
  };
}

/**
 * Whether the picker can reset the session's model to its default. Configured
 * sessions reset through their saved policy; native resets otherwise require a
 * harness that can clear its live model override.
 */
export function modelResetSupported({
  inferenceConfigured,
  modelPickerKind,
  isNativeWrapper,
}: {
  inferenceConfigured?: boolean;
  modelPickerKind: NativeModelPickerKind | null;
  isNativeWrapper: boolean;
}): boolean {
  return (
    !!inferenceConfigured ||
    (modelPickerKind
      ? ["opencode", "acp", "configured"].includes(modelPickerKind)
      : !isNativeWrapper)
  );
}

/** The routing guard's session: the snapshot, or a seed for a chat not created yet. */
type CostRoutingSession = Parameters<typeof isCostRoutingEligible>[1];

export interface SessionPickerInputOptions {
  /** The capabilities the gates read; defaults to the session's own. */
  capabilitySource?: SessionCapabilitySource;
  /** The session the routing guard reads; defaults to `session`. */
  routingSession?: CostRoutingSession;
  /** The host the routing guard reads gateway backing from; defaults to the session's host. */
  routingHostId?: string | null;
  /** Models a launching sandbox offers before its host is bound. */
  launchPreviewOptions?: readonly NativeModelOption[];
}

/** What a session-scoped picker reads from one conversation's live state. */
export interface SessionPickerInputs {
  costRoutingEligible: boolean;
  modelPickerKind: NativeModelPickerKind | null;
  showModels: boolean;
  effortLevels: readonly string[];
  showEffort: boolean;
  codexModelOptions: readonly NativeModelOption[];
  modelLabelOptions: readonly NativeModelOption[];
  modelsFromLaunchPreview: boolean;
  inferenceConfigured: boolean | undefined;
  inferenceError: string | null | undefined;
}

/** The catalog harness a pre-launch host probe reads, per native picker family. */
const HOST_PROBE_HARNESS: Partial<Record<NativeModelPickerKind, string>> = {
  codex: "codex-native",
  claude: "claude-native",
  devin: "devin-native",
};

/**
 * The picker inputs for *sessionId*'s conversation, or the active one when
 * omitted. *session* is that conversation's snapshot; the full-screen view
 * layers its own fallbacks through *options*.
 */
export function useSessionPickerInputs(
  sessionId: string | undefined,
  session: Session | null | undefined,
  options: SessionPickerInputOptions = {},
): SessionPickerInputs {
  const serverInfo = useServerInfo();
  const { data: hostRows } = useHosts();
  const harnessEffortFamilies = useHarnessEffortFamilies();
  const sessionModelOptions = useSessionField(sessionId, (state) => state.codexModelOptions);
  const llmModel = useSessionField(sessionId, (state) => state.llmModel);
  const sessionModelOverride = useSessionField(sessionId, (state) => state.sessionModelOverride);
  const sessionCapabilities = useMemo(() => capabilitySourceForSession(session), [session]);
  const capabilitySource = options.capabilitySource ?? sessionCapabilities;
  const routingSession = options.routingSession !== undefined ? options.routingSession : session;
  const routingHostId =
    options.routingHostId !== undefined ? options.routingHostId : (session?.hostId ?? null);
  const routingHost = hostRows?.find((row) => row.host_id === routingHostId) ?? null;
  const costRoutingEligible = isCostRoutingEligible(serverInfo, routingSession, routingHost);

  // Pre-catalog fallback: a fresh native session's own catalog arrives only once
  // its CLI is up, so ride the host's cached probe rows until the runner's lands.
  const fallbackPickerKind = modelPickerKindForConv({
    labels: session?.labels ?? {},
    harness: session?.harness ?? null,
  });
  const hostProbeHarness =
    fallbackPickerKind === null ? null : (HOST_PROBE_HARNESS[fallbackPickerKind] ?? null);
  const { data: hostProbeOptions } = useHostModelOptions(
    session?.hostId ?? null,
    hostProbeHarness ?? "",
    !session?.inferenceConfigured && hostProbeHarness !== null && sessionModelOptions.length === 0,
  );
  const preLaunchOptions =
    hostProbeOptions != null && hostProbeOptions.length > 0
      ? hostProbeOptions
      : options.launchPreviewOptions;
  // Keep the store's array reference unless pre-launch rows exist: a fresh []
  // would re-render every options consumer on each streaming/liveness tick.
  const codexModelOptions =
    !session?.inferenceConfigured &&
    sessionModelOptions.length === 0 &&
    preLaunchOptions != null &&
    preLaunchOptions.length > 0
      ? preLaunchOptions
      : sessionModelOptions;
  const modelsFromLaunchPreview =
    options.launchPreviewOptions !== undefined &&
    codexModelOptions === options.launchPreviewOptions;

  const modelPickerKind = modelPickerKindForConv(capabilitySource, codexModelOptions);
  // Effort ladders key on the model the session is actually on: the reported
  // `llmModel`, then the pinned `model_override` (devin pins its family there).
  const sdkEffortFamily = sdkEffortFamilyForHarness(
    capabilitySource.harness,
    harnessEffortFamilies,
  );
  const effortLevels = useMemo(
    () =>
      effortLevelsForConv(
        capabilitySource,
        codexModelOptions,
        llmModel ?? sessionModelOverride,
        sdkEffortFamily,
      ),
    [capabilitySource, codexModelOptions, llmModel, sessionModelOverride, sdkEffortFamily],
  );
  const showEffort =
    shouldShowEffortPicker(capabilitySource, sdkEffortFamily) && effortLevels.length > 0;
  return {
    costRoutingEligible,
    modelPickerKind,
    showModels: modelPickerKind !== null,
    effortLevels,
    showEffort,
    codexModelOptions,
    modelLabelOptions: sessionModelOptions,
    modelsFromLaunchPreview,
    inferenceConfigured: session?.inferenceConfigured,
    inferenceError: session?.inferenceError,
  };
}
