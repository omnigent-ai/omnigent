// Sub-agent tabs share the Workspace panel's tab list with side chats. The
// prefix tells the rail to show the whole session and not stop it on close.

const SUBAGENT_TAB_PREFIX = "subagent:";

/** The chat-tab id for a sub-agent session. */
export function subagentTabId(childSessionId: string): string {
  return `${SUBAGENT_TAB_PREFIX}${childSessionId}`;
}

/** The sub-agent session behind a chat-tab id, or null for a side-chat tab. */
export function subagentSessionIdFromTab(tabId: string): string | null {
  if (!tabId.startsWith(SUBAGENT_TAB_PREFIX)) return null;
  const sessionId = tabId.slice(SUBAGENT_TAB_PREFIX.length);
  return sessionId.length > 0 ? sessionId : null;
}
