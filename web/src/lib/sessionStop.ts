// Gate for the sidebar row kebab's "Stop session" item.

const WRAPPER_LABEL_KEY = "omnigent.wrapper";
const CLAUDE_NATIVE_WRAPPER_VALUE = "claude-code-native-ui";

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
  const isClaudeNative = opts.labels?.[WRAPPER_LABEL_KEY] === CLAUDE_NATIVE_WRAPPER_VALUE;
  const isHostSpawned = Boolean(opts.hostId && opts.runnerId);
  return isClaudeNative || isHostSpawned;
}
