// Gate for the sidebar row kebab's "Stop session" item.

const WRAPPER_LABEL_KEY = "omnigent.wrapper";
const CLAUDE_NATIVE_WRAPPER_VALUE = "claude-code-native-ui";
const DEVIN_NATIVE_WRAPPER_VALUE = "devin-native-ui";
// Wrappers whose runner can kill the harness itself (its tmux pane) even when the
// runner is local and there is nothing to kill via a host: claude-native is
// special-cased runner-side, and devin-native rides the uniform stop map
// (`_UNIFORM_STOP` in `omnigent/runner/native/interrupt.py`). The map's other
// members (cursor, goose, hermes, kimi, kiro, qwen) are candidates to add once
// their stop path has actually been exercised — listing a wrapper here whose stop
// does not land would leave a dead menu item.
const STOPPABLE_WRAPPERS: ReadonlySet<string> = new Set([
  CLAUDE_NATIVE_WRAPPER_VALUE,
  DEVIN_NATIVE_WRAPPER_VALUE,
]);

/**
 * Whether the web UI can kill a session's runner. True for host-spawned
 * runners (hostId + runnerId — server kills it via the host, any harness)
 * or claude-native (kills its tmux pane). A local in-process runner (no
 * host) is a no-op server-side, so it stays false / hidden.
 */
export function isSessionStoppable(opts: {
  labels: Record<string, string> | undefined;
  hostId: string | null | undefined;
  runnerId: string | null | undefined;
}): boolean {
  const isStoppableWrapper = STOPPABLE_WRAPPERS.has(opts.labels?.[WRAPPER_LABEL_KEY] ?? "");
  const isHostSpawned = Boolean(opts.hostId && opts.runnerId);
  return isStoppableWrapper || isHostSpawned;
}
