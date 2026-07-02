// Pure helpers for the "fork / switch agent" flows: decide which target
// harnesses preserve the source's conversation history (and so should appear
// in each picker). Three carry mechanisms, all keyed off the TARGET harness —
// a frontend mirror of the server (omnigent/server/routes/sessions.py):
//
//   - SDK (non-native) harnesses replay the Omnigent transcript as LLM
//     context — carry on BOTH fork and switch, regardless of source/family.
//   - Native-REBUILD harnesses (Claude Code, Codex, Pi, Hermes, Qwen Code)
//     record a resumable on-disk session file the runner rebuilds from the
//     copied Omnigent items (a same-family native source clones the source
//     file instead; cross-family rebuilds, format-agnostic) — carry on BOTH
//     fork and switch. Mirrors `_FORK_HISTORY_NATIVE_HARNESSES`.
//   - PREAMBLE harnesses (Cursor, OpenCode) have no rebuildable local store,
//     so prior turns replay as a text preamble on the fork's first message —
//     FORK-ONLY. An in-place switch-agent has no first-message injection
//     point, so switching into one starts fresh. Mirrors
//     `_CURSOR_FORK_HISTORY_HARNESSES`.
//
// Hence two predicates: forkTargetCarriesHistory (rebuild ∪ preamble ∪ SDK)
// and switchTargetCarriesHistory (rebuild ∪ SDK — no preamble). Native
// harnesses with no carry path (kiro/kimi/goose) are offered by neither; an
// unclassifiable harness (catalog harness=null) is conservatively dropped.

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
 * Whether a harness is a native CLI harness that carries fork/switch history
 * (Claude Code / Codex / Cursor / Pi / Antigravity / Qwen Code). These are the
 * native harnesses whose history the runner rebuilds or replays on a fork — the
 * subset of Python `NATIVE_HARNESSES` (`omnigent/harness_aliases.py`) that the
 * server gates in `_FORK_HISTORY_NATIVE_HARNESSES` /
 * `_CURSOR_FORK_HISTORY_HARNESSES` (`server/routes/sessions.py`). A native
 * harness that always starts fresh (e.g. goose-native) is intentionally absent
 * so the picker doesn't promise history it would drop. Both native-antigravity
 * spellings are included (the in-process `antigravity` SDK harness is NOT
 * native); qwen-native rebuilds qwen's on-disk chat recording from the copied
 * Omnigent items (see `write_qwen_session_recording`).
 */
export function isNativeHarness(harness: string | null | undefined): boolean {
  return (
    harness === "claude-native" ||
    harness === "native-claude" ||
    harness === "codex-native" ||
    harness === "native-codex" ||
    harness === "cursor-native" ||
    harness === "native-cursor" ||
    harness === "pi-native" ||
    harness === "native-pi" ||
    harness === "antigravity-native" ||
    harness === "native-antigravity" ||
    harness === "qwen-native" ||
    harness === "native-qwen"
  );
}

/**
 * Native harnesses whose runner REBUILDS a resumable on-disk transcript from
 * the copied Omnigent items, so they carry history on BOTH fork and switch.
 * Mirrors Python `_FORK_HISTORY_NATIVE_HARNESSES`
 * (`omnigent/server/routes/sessions.py`); both canonical and reversed
 * spellings are listed so a catalog `harness` in either form matches.
 */
const NATIVE_REBUILD_HARNESSES: ReadonlySet<string> = new Set([
  "claude-native",
  "native-claude",
  "codex-native",
  "native-codex",
  "pi-native",
  "native-pi",
  "hermes-native",
  "native-hermes",
  "qwen-native",
  "native-qwen",
]);

/**
 * Native harnesses that carry FORK history only as a text preamble on the
 * fork's first message (no rebuildable local store) — FORK-ONLY. An in-place
 * switch-agent has no first-message injection point, so switching into one
 * starts fresh. Mirrors Python `_CURSOR_FORK_HISTORY_HARNESSES`.
 */
const PREAMBLE_FORK_HARNESSES: ReadonlySet<string> = new Set([
  "cursor-native",
  "native-cursor",
  "opencode-native",
  "native-opencode",
]);

/**
 * Whether forking into `targetHarness` keeps the source's conversation
 * history (and so should be offered in the fork picker): a native-rebuild
 * target, a preamble (cursor/opencode) target, or an SDK-family target (which
 * replays the transcript as context). Conservatively false for an
 * unclassifiable harness (the catalog can report harness=null when the bundle
 * failed to load) and for native harnesses with no carry path
 * (kiro/kimi/goose).
 *
 * NOTE: the SDK branch is `harnessFamily(h) !== null`, which also matches the
 * one native harness that has a single family today — antigravity-native
 * (gemini). That preserves Antigravity's prior presence in the pickers, but a
 * native Antigravity fork is in NEITHER server carry-set, so whether it truly
 * carries is unverified. TODO(fork-switch): confirm, then move it to a carry
 * set or drop it rather than leaning on this proxy.
 *
 * @param targetHarness - The harness the fork would bind.
 */
export function forkTargetCarriesHistory(targetHarness: string | null | undefined): boolean {
  if (!targetHarness) return false;
  return (
    NATIVE_REBUILD_HARNESSES.has(targetHarness) ||
    PREAMBLE_FORK_HARNESSES.has(targetHarness) ||
    harnessFamily(targetHarness) !== null
  );
}

/**
 * Whether switching a session in place to `targetHarness` keeps history (and
 * so should be offered in the switch-agent picker). Same as
 * {@link forkTargetCarriesHistory} MINUS the preamble harnesses: the
 * text-preamble path is fork-only (switch-agent has no first-message
 * injection point), so a switch into cursor/opencode would silently start
 * fresh and must not be offered. Mirrors the server, where switch-agent stamps
 * carry-history only for `_FORK_HISTORY_NATIVE_HARNESSES`, never the cursor
 * set.
 *
 * @param targetHarness - The harness the switch would bind.
 */
export function switchTargetCarriesHistory(targetHarness: string | null | undefined): boolean {
  if (!targetHarness) return false;
  return NATIVE_REBUILD_HARNESSES.has(targetHarness) || harnessFamily(targetHarness) !== null;
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
