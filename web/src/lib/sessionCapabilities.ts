/** UI-only session capability gates, derived from snapshot labels. */

const CLAUDE_NATIVE_WRAPPER = "claude-code-native-ui";
const CODEX_NATIVE_WRAPPER = "codex-native-ui";

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
  session: { labels?: Record<string, string | null> | null } | null | undefined,
): boolean {
  const wrapper = session?.labels?.["omnigent.wrapper"];
  return wrapper === CLAUDE_NATIVE_WRAPPER || wrapper === CODEX_NATIVE_WRAPPER;
}
