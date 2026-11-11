// How a session's Chat/TUI view choice is stored per conversation and read back.

// `null` in panel state means Chat view, but an absent sessionStorage entry
// means "use the configured default." Keep an explicit marker so choosing Chat
// can override a Terminal default when the user returns to that conversation.
export const CHAT_VIEW_STORAGE_VALUE = "__chat__";

/** The sessionStorage key holding a conversation's Chat/TUI choice. */
export function panelKeyStorageKey(conversationId: string): string {
  return `omnigent.web.panel-key:${conversationId}`;
}

/**
 * Resolve the terminal-view target for `?view=terminal`.
 *
 * The URL param names only the SURFACE (chat vs terminal), never which
 * terminal, so restoring straight from it would rewrite a rail-opened shell
 * to the agent pane. The precise target lives in the per-session store, which
 * the panel setter writes alongside the param — prefer it, and fall back to
 * the agent terminal for a deep link that carries no stored target.
 *
 * :param stored: Per-session stored panel key (null when absent).
 * :param agentKey: The session's agent-terminal key (or the no-target sentinel).
 * :returns: The terminal key the view should open on.
 */
export function resolveTerminalViewKey(stored: string | null, agentKey: string): string {
  return stored !== null && stored !== CHAT_VIEW_STORAGE_VALUE ? stored : agentKey;
}
