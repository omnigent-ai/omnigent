import { getSessionSlim } from "@/lib/sessionsApi";
import { AGENT_ACTION_EXPIRED_ERROR } from "./deadline";
import { BROWSER_AGENT_TOOLS_LABEL, readBrowserAgentToolsEnabled } from "./preferences";

/** Relay actions that only run while the switch is on. */
const ADVANCED_ACTIONS = new Set([
  "go_back",
  "go_forward",
  "reload",
  "tabs",
  "hover",
  "drag",
  "scroll",
  "press_key",
  "select_option",
  "check",
]);

function isAdvancedClick(action: string, args: Record<string, unknown>): boolean {
  return (
    action === "click" &&
    ((args.button !== undefined && args.button !== "left") || args.double_click === true)
  );
}

/** The tool an advanced action is, for messages; null for a basic one. */
function advancedTool(action: string, args: Record<string, unknown>): string | null {
  if (isAdvancedClick(action, args)) return "browser_click with button or double_click";
  return ADVANCED_ACTIONS.has(action) ? `browser_${action}` : null;
}

// Sessions' browser tools labels. A label is set when the session is created
// and never changes (the server refuses it from clients, on create and in
// updates), so each session is looked up once.
const sessionLabels = new Map<string, string | null>();

/** The session's label, or the error to return for `tool` instead. The
 *  lookup ends at `deadlineAt`. */
async function sessionLabel(
  sessionId: string,
  tool: string,
  deadlineAt: number,
): Promise<{ label: string | null } | { error: string }> {
  const known = sessionLabels.get(sessionId);
  if (known !== undefined) return { label: known };
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const session = await Promise.race([
      getSessionSlim(sessionId),
      new Promise<null>((resolve) => {
        timer = setTimeout(() => resolve(null), Math.max(0, deadlineAt - Date.now()));
      }),
    ]);
    if (session === null || Date.now() >= deadlineAt) return { error: AGENT_ACTION_EXPIRED_ERROR };
    const label = session.labels?.[BROWSER_AGENT_TOOLS_LABEL] ?? null;
    sessionLabels.set(sessionId, label);
    return { label };
  } catch {
    return { error: `${tool} couldn't check this session's settings; try again` };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * The error to return instead of running an advanced `action` in a session
 * that wasn't created with the advanced tools, or null when it may run.
 * The runner runs any browser tool by name, so a model naming a tool its
 * session never offered (a prompt injection, say) would otherwise reach the
 * page whenever this window's switch is on.
 */
export async function advancedToolsNotInSessionError(
  sessionId: string,
  action: string,
  args: Record<string, unknown>,
  deadlineAt: number,
): Promise<string | null> {
  const tool = advancedTool(action, args);
  if (tool === null) return null;
  const found = await sessionLabel(sessionId, tool, deadlineAt);
  if ("error" in found) return found.error;
  if (found.label === "on") return null;
  return `${tool} isn't available in this session: it was started without the advanced browser tools. Start a new session with Settings > General > Advanced browser tools for agents on.`;
}

/**
 * In a desktop app without the advanced tools (an older one): the error to
 * return instead of running `action`, or null to run the standard tools. Sessions
 * created in an app with them were promised its tools, and its trusted
 * click and type, which this app can't keep.
 */
export async function olderDesktopError(
  sessionId: string,
  action: string,
  args: Record<string, unknown>,
  deadlineAt: number,
): Promise<string | null> {
  const advanced = advancedTool(action, args);
  if (advanced) {
    return `this Omnigent desktop app doesn't support ${advanced}; update the app to use it`;
  }
  const tool = `browser_${action}`;
  const found = await sessionLabel(sessionId, tool, deadlineAt);
  if ("error" in found) return found.error;
  if (found.label !== "on" && found.label !== "trusted") return null;
  return `this session was started in a newer Omnigent desktop app; update this app to use ${tool} here, or start a new session in it`;
}

/** The error to return instead of running `action`, or null when it may run.
 *  Agents only see these tools in sessions created with the switch on; this
 *  also covers a switch turned off after such a session started. */
export function browserAgentToolsOffError(
  action: string,
  args: Record<string, unknown>,
): string | null {
  if (readBrowserAgentToolsEnabled()) return null;
  const tool = advancedTool(action, args);
  if (tool === null) return null;
  return `${tool} is turned off. The user can turn on Settings > General > Advanced browser tools for agents, then start a new session.`;
}
