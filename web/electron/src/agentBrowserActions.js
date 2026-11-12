"use strict";

/**
 * Main-process handling of the agent browser tools beyond
 * the standard snapshot / click / type page scripts, for the
 * `omnigent:browser-perform-action` IPC handler.
 *
 * - back / forward / reload go through the view registry's history navigation;
 * - click, type, hover, drag, scroll, press_key and check send trusted input
 *   (browserInput.js);
 * - select_option runs a page script: a native dropdown is an OS menu that
 *   protocol input can't drive;
 * - check_page (browser_tabs select, before it chooses a tab) refuses a page
 *   the issuing session's agent couldn't open itself, such as a loopback or
 *   internal page the user opened from the address bar.
 */

const { buildAgentActionScript } = require("./agentActionScripts.mjs");
const { beforeAgentDeadline, DEFAULT_ACTION_MS } = require("./agentDeadline");
const { needsTrustedInput, performBrowserInput, runActionScript } = require("./browserInput");

const HISTORY_ACTIONS = new Set(["go_back", "go_forward", "reload"]);
const PAGE_SCRIPT_ACTIONS = new Set(["select_option"]);

/**
 * Perform an agent browser action, or return null for browserIpc's own handler.
 *
 * Every action first gets a laid-out viewport, so one sent to a tab the user
 * hasn't shown still finds the page's elements where they render.
 *
 * @param {object} registry The window's browser view registry.
 * @param {{ view: { webContents: any } }} entry The view's registry entry.
 * @param {string} conversationId The view id.
 * @param {string} action e.g. "drag" or "go_back".
 * @param {Record<string, unknown> | undefined} params The action's arguments.
 * @param {{ agentContextFor?: (sourceHostId: string | null) => unknown,
 *   deadlineAt?: number }} [deps]
 *   `agentContextFor` builds main's agent context for the requesting window
 *   and the issuing session's host (browserIpc's getAgentContextForEvent).
 *   `deadlineAt` is the caller's (epoch ms): the action stops there.
 * @returns {Promise<{ ok: boolean, result?: string, error?: string } | null>}
 *   The IPC result; throws on action errors, like browserIpc's own path.
 */
async function performAgentBrowserAction(
  registry,
  entry,
  conversationId,
  action,
  params,
  { agentContextFor, deadlineAt = Date.now() + DEFAULT_ACTION_MS } = {},
) {
  registry.ensureAgentViewport?.(conversationId);
  // The relay's provenance (the issuing session's host) rides with the
  // action; it isn't one of the action's own arguments.
  const { provenance, ...actionParams } = params ?? {};
  // The issuing session's host, never the tab's: an address-bar page's
  // context may allow what this session's navigations can't.
  const source = typeof provenance?.sourceHostId === "string" ? provenance.sourceHostId : null;
  const agentContext = agentContextFor ? agentContextFor(source) : { agentRequested: true };
  if (action === "check_page") {
    if (Object.keys(actionParams).length > 0) {
      throw new Error("Unsupported browser action argument");
    }
    const url = entry.view.webContents.getURL();
    if (!url || url === "about:blank") return { ok: true, result: "{}" };
    const verdict = await beforeAgentDeadline(
      Promise.resolve(registry.checkAgentNavigation(conversationId, url, agentContext)),
      deadlineAt,
    );
    if (!verdict?.ok) {
      return {
        ok: false,
        error: `this tab shows a page agents in this session can't open (${verdict?.error ?? "refused"}), so it wasn't selected`,
      };
    }
    return { ok: true, result: "{}" };
  }
  if (HISTORY_ACTIONS.has(action)) {
    if (Object.keys(actionParams).length > 0) {
      throw new Error("Unsupported browser action argument");
    }
    const r = await registry.agentNavigateHistory(conversationId, action, {
      deadlineAt,
      agentContext,
    });
    if (!r.ok) return { ok: false, error: r.error };
    return { ok: true, result: JSON.stringify({ url: r.url, title: r.title }) };
  }
  const input = needsTrustedInput(action);
  if (input || PAGE_SCRIPT_ACTIONS.has(action)) {
    // An agent's input can navigate (a link, a form, Enter). Whatever the
    // tab was, from here its navigations get the agent policy for the
    // issuing session, like the agent's own navigations: a tab the user
    // opened from the address bar may otherwise reach loopback or internal
    // hosts this session can't. An older web bundle sends no
    // provenance: then a context an agent navigation already set here (with
    // the session's host, so a localhost preview's links keep working) stays,
    // and only a tab without one gets the host-less context.
    const keep = provenance === undefined && entry.navigationPolicyEnforced && entry.agentContext;
    if (!keep) {
      entry.navigationPolicyEnforced = true;
      entry.agentContext = agentContext;
    }
  }
  if (input) {
    const result = await performBrowserInput(entry, action, actionParams, { deadlineAt });
    return { ok: true, result: JSON.stringify(result) };
  }
  if (PAGE_SCRIPT_ACTIONS.has(action)) {
    const result = await runActionScript(
      entry.view.webContents,
      buildAgentActionScript(action, actionParams),
      { deadlineAt },
    );
    return { ok: true, result: typeof result === "string" ? result : JSON.stringify(result) };
  }
  return null;
}

/** The view's page title, or "" once it's gone. */
function viewTitle(webContents) {
  try {
    return webContents.getTitle();
  } catch {
    return "";
  }
}

module.exports = { performAgentBrowserAction, viewTitle };
