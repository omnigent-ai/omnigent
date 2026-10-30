/** Stable soft-tab ID for the browser view driven by agents. */
export const AGENT_BROWSER_TAB_ID = "agent-browser";

export function browserViewId(conversationId: string, tabId: string): string {
  return tabId === AGENT_BROWSER_TAB_ID
    ? conversationId
    : `browser-tab:${encodeURIComponent(conversationId)}:${tabId}`;
}

/** Owning session ID for a browser view ID; mirrors the Electron view-registry parser. */
export function browserViewOwnerId(viewId: string): string {
  const tab = /^browser-tab:([^:]+):[^:]+$/.exec(viewId);
  if (!tab) return viewId;
  try {
    return decodeURIComponent(tab[1]);
  } catch {
    return viewId;
  }
}
