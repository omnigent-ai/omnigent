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
export function harnessFamily(
  harness: string | null | undefined,
): "anthropic" | "openai" | "gemini" | null {
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
    // Antigravity is Gemini-family: the native CLI (`antigravity-native`)
    // and the in-process SDK (`antigravity`, plus reversed spellings) all
    // consume Gemini models.
    case "antigravity-native":
    case "native-antigravity":
    case "antigravity":
      return "gemini";
    default:
      return null;
  }
}

/**
 * Whether a harness is a native CLI harness (Claude Code / Codex / Pi /
 * Antigravity). Mirrors Python `NATIVE_HARNESSES` (`omnigent/harness_aliases.py`)
 * — including both native-antigravity spellings (the in-process `antigravity`
 * SDK harness is NOT native) — so both sides classify the same set.
 */
export function isNativeHarness(harness: string | null | undefined): boolean {
  return (
    harness === "claude-native" ||
    harness === "native-claude" ||
    harness === "codex-native" ||
    harness === "native-codex" ||
    harness === "pi-native" ||
    harness === "native-pi" ||
    harness === "antigravity-native" ||
    harness === "native-antigravity"
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
 * Strip ONE trailing `" (fork <id>)"` / `" (switch <id>)"` suffix.
 *
 * Internal one-layer primitive for {@link agentRootName}; not exported,
 * because a fork of a fork stacks these suffixes and every caller that
 * matches a clone name back to its origin (built-in catalog, native-label
 * map, switch-dialog dedup) wants the FULLY rooted name. Reaching for a
 * single-layer strip is the footgun that lets a multi-fork clone slip the
 * match — so callers use `agentRootName`, never this.
 *
 * @param name - An agent name, e.g. `"claude-native-ui (fork conv_ab12)"`.
 * @returns The name with one clone suffix removed.
 */
function agentBaseName(name: string): string {
  return name.replace(/ \((?:fork|switch) [^)]+\)$/, "");
}

/**
 * The root agent name behind ANY chain of fork/switch clone suffixes.
 *
 * The fork/switch routes clone a bound agent as `"<name> (fork <id>)"`, and
 * a fork of a fork accumulates them — e.g. `"claude-native-ui (fork ag_a)
 * (fork ag_b)"`. This peels EVERY layer to the root, so a clone (however
 * deep) still matches the agent it derives from by name.
 *
 * Use this for ALL clone-name → catalog matching: the new-session picker
 * dropping session agents that shadow a built-in (`useAvailableAgents`),
 * the in-session model-picker / agent-info label (`agentDisplayLabel`), and
 * the switch-agent dialog excluding the current agent's origin. A
 * single-layer strip would leave `"claude-native-ui (fork ag_a)"`, miss the
 * match, and surface the clone as a spurious "custom" agent / duplicate
 * built-in / raw suffixed label.
 *
 * @param name - An agent name, possibly with nested clone suffixes.
 * @returns The root base name with all clone suffixes removed.
 */
export function agentRootName(name: string): string {
  let prev: string;
  let cur = name;
  do {
    prev = cur;
    cur = agentBaseName(cur);
  } while (cur !== prev);
  return cur;
}
