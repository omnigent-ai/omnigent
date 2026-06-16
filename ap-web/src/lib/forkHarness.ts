// Pure helpers for the "fork with a different agent" flow: decide which
// switch targets preserve the source's conversation history.
//
// Two mechanisms carry a fork's history, both keyed off the TARGET harness:
//   - SDK (non-native) harnesses replay the Omnigent transcript as LLM
//     context, so they always carry history regardless of the source.
//   - Native harnesses (Claude Code, Codex) do NOT replay the transcript;
//     the runner rebuilds their on-disk transcript before launch — cloning
//     the source's native transcript when the source is same-family native,
//     else building one from the copied Omnigent items (a format-agnostic
//     conversion, so the source harness doesn't matter).
//
// Native targets carry history from any source: the rollout synthesizer
// writes the session_meta fields codex ≥ 0.133 requires (timestamp,
// cli_version, model_provider) plus the event_msg mirrors codex rebuilds
// visible turns from, so cross-family forks into codex-native rebuild the
// rollout from the copied Omnigent items like claude-native always did
// (see _codex_rollout_records_from_session_items in omnigent/codex_native.py
// and tests/e2e/test_host_cross_family_fork_e2e.py).

/** Provider family a harness consumes, or null when unknown. */
export function harnessFamily(harness: string | null | undefined): "anthropic" | "openai" | null {
  if (!harness) return null;
  switch (harness) {
    case "claude-native":
    case "native-claude":
    case "claude-sdk":
    case "claude_sdk":
      return "anthropic";
    case "codex":
    case "codex-native":
    case "native-codex":
    case "openai-agents":
    case "openai-agents-sdk":
    case "agents_sdk":
      return "openai";
    default:
      return null;
  }
}

/** Whether a harness is a native CLI harness (Claude Code / Codex / Pi). */
export function isNativeHarness(harness: string | null | undefined): boolean {
  return (
    harness === "claude-native" ||
    harness === "native-claude" ||
    harness === "codex-native" ||
    harness === "native-codex" ||
    harness === "pi-native" ||
    harness === "native-pi"
  );
}

/**
 * Whether forking/switching into `targetHarness` keeps the source's
 * conversation history (and so should be offered in the picker).
 *
 * True for every classifiable target — the source harness doesn't matter:
 *   - an SDK target replays the transcript as context;
 *   - a native target clones the source's native transcript when the
 *     source is same-family native, else the runner rebuilds the target's
 *     on-disk transcript from the copied Omnigent items (a format-agnostic
 *     conversion; see the module comment).
 *
 * Returns false — conservatively — only for a target whose harness we
 * can't classify.
 *
 * TODO(fork-switch): the false-for-unknown default exists because the
 * catalog can report `harness: null` when the server couldn't load the
 * agent's bundle (see `_to_agent_object` in
 * `server/routes/builtin_agents.py`). We don't offer a switch we can't
 * verify preserves history. Revisit once the catalog reliably reports a
 * harness for every built-in, or to add an explicit "may start fresh"
 * affordance for unclassified harnesses.
 *
 * @param targetHarness - The harness the fork would switch to.
 */
export function forkTargetCarriesHistory(targetHarness: string | null | undefined): boolean {
  // Gate on isNativeHarness too: Pi is native but multi-family, so its
  // harnessFamily is null and it would otherwise be dropped from the pickers.
  return isNativeHarness(targetHarness) || harnessFamily(targetHarness) !== null;
}

/**
 * Strip the `" (fork <id>)"` / `" (switch <id>)"` suffix the fork/switch
 * routes append to an agent's name when cloning it, so a clone can be
 * matched back to the agent it was cloned from by name.
 *
 * @param name - An agent name, e.g. `"claude-native-ui (fork conv_ab12)"`.
 * @returns The base name, e.g. `"claude-native-ui"`. Names without a
 *   clone suffix are returned unchanged.
 */
export function agentBaseName(name: string): string {
  return name.replace(/ \((?:fork|switch) [^)]+\)$/, "");
}
