/** The switch for the advanced embedded-browser agent tools (history,
 *  tabs, hover, drag, scroll, keys, selects, checkboxes, and right / double
 *  click). Per device and on by default. The web app sends it as a header
 *  when it creates a session; the server stores it on the session and the
 *  runner hides the tools from agents in sessions created while it was off
 *  (see omnigent/tools/browser_agent_tools.py). */

export const BROWSER_AGENT_TOOLS_STORAGE_KEY = "omnigent:browser-agent-tools";
export const BROWSER_AGENT_TOOLS_HEADER = "X-Omnigent-Browser-Agent-Tools";
/** The session label the server records the header as ("on" / "trusted"). */
export const BROWSER_AGENT_TOOLS_LABEL = "omnigent.browser_agent_tools";

export function readBrowserAgentToolsEnabled(): boolean {
  try {
    // On unless turned off here.
    return window.localStorage.getItem(BROWSER_AGENT_TOOLS_STORAGE_KEY) !== "off";
  } catch {
    return true;
  }
}

export function writeBrowserAgentToolsEnabled(enabled: boolean): void {
  try {
    window.localStorage.setItem(BROWSER_AGENT_TOOLS_STORAGE_KEY, enabled ? "on" : "off");
  } catch {
    // localStorage failures should not make Settings unusable.
  }
}

/** Whether this desktop app can run the advanced tools: it keeps the
 *  sessions' browser tabs in its main process (one page per tab across
 *  windows) and clicks and types with trusted input. Older apps (and plain
 *  browsers) run the standard browser tools only. */
export function desktopSupportsBrowserAgentTools(): boolean {
  const bridge = (window as { omnigentDesktop?: { browserTabs?: unknown } }).omnigentDesktop;
  return typeof bridge?.browserTabs === "function";
}

/** Whether the advanced tools are on here: switched on, in an app that has them. */
export function browserAgentToolsActive(): boolean {
  return readBrowserAgentToolsEnabled() && desktopSupportsBrowserAgentTools();
}

/** Headers for requests that may create a session: "on" or "trusted" from
 *  a desktop app with the tools, so sessions created anywhere else keep
 *  the standard click and type contract. */
export function browserAgentToolsRequestHeaders(): Record<string, string> {
  if (!desktopSupportsBrowserAgentTools()) return {};
  return { [BROWSER_AGENT_TOOLS_HEADER]: readBrowserAgentToolsEnabled() ? "on" : "trusted" };
}
