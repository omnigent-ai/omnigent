/** UI-only session capability gates, derived from the live session snapshot. */

export const ARCLET_FORK_UNSUPPORTED = "Forking Arclet sessions is not supported yet.";
export const ARCLET_SWITCH_HOST_UNSUPPORTED =
  "Switching hosts is not supported for Arclet sessions yet.";
export const SESSION_ACTIONS_LOADING = "Checking session capabilities…";

export interface SessionActionSource {
  labels?: Record<string, string | null> | null;
  hostId?: string | null;
  host_id?: string | null;
}

export function sessionActionRestrictions(
  session: SessionActionSource | null | undefined,
  host?: { sandbox_provider?: string | null },
): { forkDisabledReason?: string; switchHostDisabledReason?: string } {
  // Embedded deployments derive this label even when the viewer cannot list
  // the source's host. A repository label alone does not imply a restriction.
  const unsupportedSource =
    session?.labels?.["omnigent.host_type"] === "managed" || host?.sandbox_provider === "arclet";
  return {
    forkDisabledReason: unsupportedSource ? ARCLET_FORK_UNSUPPORTED : undefined,
    switchHostDisabledReason: unsupportedSource
      ? ARCLET_SWITCH_HOST_UNSUPPORTED
      : host?.sandbox_provider
        ? "Switching hosts is not supported for managed sandbox sessions yet."
        : undefined,
  };
}

const CLAUDE_NATIVE_WRAPPER = "claude-code-native-ui";
const CODEX_NATIVE_WRAPPER = "codex-native-ui";
const PI_NATIVE_WRAPPER = "pi-native-ui";
const DEVIN_NATIVE_WRAPPER = "devin-native-ui";

/**
 * Fail-closed gate for Web UI reasoning-effort controls.
 *
 * :param session: Session or sidebar row carrying labels. ``null`` or missing
 *     labels fail closed.
 * :returns: True only for native sessions with Web UI effort controls.
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
      }
    | null
    | undefined,
): boolean {
  const wrapper = session?.labels?.["omnigent.wrapper"];
  return (
    wrapper === CLAUDE_NATIVE_WRAPPER ||
    wrapper === CODEX_NATIVE_WRAPPER ||
    wrapper === PI_NATIVE_WRAPPER ||
    // Devin has no --effort flag: effort is a model-variant suffix the executor
    // recombines and re-applies via /model, so the in-chat effort dial is live.
    wrapper === DEVIN_NATIVE_WRAPPER ||
    (wrapper == null && session?.harness === "codex-native")
  );
}
