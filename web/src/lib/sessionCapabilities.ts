/** UI-only session capability gates, derived from the live session snapshot. */

export const SANDBOX_FORK_UNSUPPORTED = "Forking this sandbox session is not supported yet.";
export const SANDBOX_SWITCH_HOST_UNSUPPORTED =
  "Switching hosts is not supported for this sandbox session yet.";
export const SESSION_ACTIONS_LOADING = "Checking session capabilities…";
export const SESSION_ACTIONS_UNAVAILABLE =
  "Unable to check session capabilities. Reload to try again.";

/**
 * Fallback wire IDs for snapshots missing the deployment's managed marker.
 * Other sandbox providers remain forkable in the core server.
 */
const UNSUPPORTED_FORK_PROVIDERS = new Set(["arclet", "lakebox"]);

export interface SessionActionSource {
  labels?: Record<string, string | null> | null;
  /** Session snapshot shape. */
  hostId?: string | null;
  /** Sidebar ConversationSummary wire shape. */
  host_id?: string | null;
}

export function sessionActionRestrictions(
  session: SessionActionSource | null | undefined,
  host?: { sandbox_provider?: string | null },
): { forkDisabledReason?: string; switchHostDisabledReason?: string } {
  // The embedded deployment derives this marker alongside its managed-fork ban,
  // regardless of provider. It is distinct from core `host_type: managed` launches.
  const unsupportedSource =
    session?.labels?.["omnigent.host_type"] === "managed" ||
    (host?.sandbox_provider != null && UNSUPPORTED_FORK_PROVIDERS.has(host.sandbox_provider));
  return {
    forkDisabledReason: unsupportedSource ? SANDBOX_FORK_UNSUPPORTED : undefined,
    switchHostDisabledReason:
      unsupportedSource || host?.sandbox_provider ? SANDBOX_SWITCH_HOST_UNSUPPORTED : undefined,
  };
}

const CLAUDE_NATIVE_WRAPPER = "claude-code-native-ui";
const CODEX_NATIVE_WRAPPER = "codex-native-ui";
const PI_NATIVE_WRAPPER = "pi-native-ui";
const DEVIN_NATIVE_WRAPPER = "devin-native-ui";

// Reasoning-effort ladder per server ``EffortFamily`` (``/v1/harnesses`` →
// ``capabilities.effort``). Mirrors ``omnigent/util/reasoning_effort.py``;
// ``codex-native`` is per-model and ``none`` has no ladder, so neither is here.
// ``ultra`` is omitted from ``pi`` as it aliases to ``max``.
const EFFORT_FAMILY_LEVELS: Readonly<Record<string, readonly string[]>> = {
  anthropic: ["low", "medium", "high", "xhigh", "max"],
  openai: ["none", "minimal", "low", "medium", "high", "xhigh"],
  gemini: ["low", "medium", "high"],
  copilot: ["low", "medium", "high", "xhigh"],
  pi: ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
};

/**
 * The effort ladder a harness's declared effort family offers.
 *
 * :param family: ``capabilities.effort`` from ``/v1/harnesses``, e.g.
 *     ``"anthropic"``; ``null`` / unknown / ``"none"`` yield no ladder.
 * :returns: Ordered effort ids, empty when the family has no ladder.
 */
export function effortLevelsForFamily(family: string | null | undefined): readonly string[] {
  return (family && EFFORT_FAMILY_LEVELS[family]) || [];
}

/**
 * Fail-closed gate for Web UI reasoning-effort controls.
 *
 * :param session: Session or sidebar row carrying labels. ``null`` or missing
 *     labels fail closed.
 * :param effortFamily: The declared effort family of the session's harness,
 *     supplied only for a NON-native harness (claude-sdk, codex, …) whose
 *     effort rides the session's ``reasoning_effort`` into the SDK executor.
 *     Native harnesses keep the wrapper-label gate below and must pass
 *     ``null`` so a label-less native session does not gain a control its
 *     bridge cannot honor.
 * :returns: True for native sessions with Web UI effort controls, and for a
 *     top-level label-less session whose harness family has an effort ladder.
 *     cursor-native is intentionally excluded: its effort lives on the /model
 *     picker's per-model "Tab to modify" axis and a model switch resets it to
 *     that model's default, so a Web UI effort dial would silently diverge from
 *     the TUI. cursor-native supports model switching only for now.
 */
export function supportsEffortControl(
  session:
    | {
        labels?: Record<string, string | null> | null;
        harness?: string | null;
        parentSessionId?: string | null;
      }
    | null
    | undefined,
  effortFamily: string | null = null,
): boolean {
  const wrapper = session?.labels?.["omnigent.wrapper"];
  if (
    wrapper === CLAUDE_NATIVE_WRAPPER ||
    wrapper === CODEX_NATIVE_WRAPPER ||
    wrapper === PI_NATIVE_WRAPPER ||
    // Devin has no --effort flag: effort is a model-variant suffix the executor
    // recombines and re-applies via /model, so the in-chat effort dial is live.
    wrapper === DEVIN_NATIVE_WRAPPER ||
    (wrapper == null && session?.harness === "codex-native")
  ) {
    return true;
  }
  // A sub-agent child takes no input of its own, so it gets no dial even when
  // its harness family has a ladder; a missing session stays fail-closed.
  return (
    session != null &&
    wrapper == null &&
    session.parentSessionId == null &&
    effortLevelsForFamily(effortFamily).length > 0
  );
}
