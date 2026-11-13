// The relay side of the agent browser tools. Wraps the relay's
// dispatch (hooks/useBrowserAgentRelay.ts): every action runs against the
// session's current agent tab, the advanced actions run here, and the rest
// go through the relay's dispatch on that tab's page. Main runs each call in
// the window that has the page (electron/src/browserTabs.js), so this
// window needn't be the one showing it.
import { AGENT_BROWSER_TAB_ID, agentBrowserTabId, browserViewId } from "@/hooks/useBrowserTabs";
import { SESSION_HOST_PROTOCOL, type BrowserHostOptions } from "@/lib/nativeBridge";
import {
  addBrowserTab,
  agentTabIn,
  BROWSER_TAB_LIMIT,
  chooseAgentBrowserTab,
  closeBrowserTab,
  discardNewBrowserTab,
} from "./agentTabs";
import { HISTORY_ACTIONS, INPUT_ACTIONS } from "./actionKinds";
import { AGENT_ACTION_BUDGET_MS, AGENT_ACTION_EXPIRED_ERROR } from "./deadline";
import { browserAgentToolsActive, desktopSupportsBrowserAgentTools } from "./preferences";
import {
  advancedToolsNotInSessionError,
  browserAgentToolsOffError,
  olderDesktopError,
  sessionTypesRealInput,
} from "./relayGate";
import { BrowserTabsExpired, loadTabs, type TabsRecord } from "./tabStore";

export interface AgentActionResult {
  ok: boolean;
  error?: string;
  data?: Record<string, unknown>;
}

/** The parts of `window.omnigentDesktop` the advanced actions call. */
interface AgentDesktopBridge {
  browserPerformAction?: (
    viewId: string,
    action: string,
    args?: Record<string, unknown>,
    opts?: BrowserHostOptions,
  ) => Promise<{ ok: boolean; result?: string; error?: string }>;
  browserHasView?: (viewId: string) => Promise<{
    exists: boolean;
    url?: string;
    title?: string;
    /** Another desktop window has the page (`url` and `title` are its). */
    elsewhere?: boolean;
  }>;
}

// Views whose last agent navigation main cancelled because a newer one (the
// user's address bar, say) took the view.
const supersededNavigations = new Set<string>();

type Bridged = (...args: unknown[]) => unknown;

/**
 * `desktop` with the action's deadline (epoch ms) on every call main does
 * page work for: actions, screenshots and navigations stop there. Also
 * notes which navigations main cancelled for a newer one (`superseded`).
 */
function boundedDesktop<D extends object>(desktop: D, deadlineAt: number): D {
  const bridge = desktop as Record<string, unknown>;
  const call = (name: string) => bridge[name] as Bridged | undefined;
  const perform = call("browserPerformAction");
  const screenshot = call("browserScreenshot");
  const open = call("browserOpenOrNavigate");
  return {
    ...desktop,
    ...(perform && {
      browserPerformAction: (
        id: string,
        action: string,
        params?: Record<string, unknown>,
        opts?: object,
      ) =>
        opts
          ? perform.call(desktop, id, action, { ...params, deadlineAt }, opts)
          : perform.call(desktop, id, action, { ...params, deadlineAt }),
    }),
    ...(screenshot && {
      browserScreenshot: (id: string, opts?: object) =>
        screenshot.call(desktop, id, { ...opts, deadlineAt }),
    }),
    ...(open && {
      browserOpenOrNavigate: async (id: string, url: string, bounds?: unknown, opts?: object) => {
        supersededNavigations.delete(id);
        const result = await open.call(desktop, id, url, bounds, { ...opts, deadlineAt });
        if ((result as { superseded?: unknown } | undefined)?.superseded === true) {
          supersededNavigations.add(id);
        }
        return result;
      },
    }),
  };
}

/** Whether main cancelled this navigation result for a newer one of its view. */
function wasSuperseded(viewId: string, result: AgentActionResult): boolean {
  return (
    (result as { superseded?: unknown }).superseded === true || supersededNavigations.has(viewId)
  );
}

/** The relay's dispatch: one action against one view. */
type Dispatch<D, R> = (
  viewId: string,
  action: string,
  args: Record<string, unknown>,
  desktop: D,
  sourceHostId: string | null,
  rememberedHostId: string | null,
  sourceIsManagedHost: boolean,
  readHostOptions?: BrowserHostOptions,
) => Promise<R>;

// Actions main performs and answers with a JSON object.
const DESKTOP_ACTIONS = new Set([
  "go_back",
  "go_forward",
  "reload",
  "hover",
  "drag",
  "scroll",
  "press_key",
  "select_option",
  "check",
]);

/** Parse main's JSON answer into the result's `data`, dropping its own `ok`. */
function actionData(result: string | undefined): Record<string, unknown> | undefined {
  try {
    const parsed: unknown = JSON.parse(result ?? "");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return undefined;
    const { ok: _ok, ...data } = parsed as Record<string, unknown>;
    return Object.keys(data).length > 0 ? data : undefined;
  } catch {
    return undefined;
  }
}

async function performDesktopAction(
  desktop: AgentDesktopBridge,
  viewId: string,
  action: string,
  args: Record<string, unknown>,
  readHostOptions?: BrowserHostOptions,
): Promise<AgentActionResult> {
  if (!desktop.browserPerformAction) {
    return { ok: false, error: "this desktop shell does not support the browser pane" };
  }
  // Host-local views are read with the session's current host, as the basic actions do.
  const r = readHostOptions
    ? await desktop.browserPerformAction(viewId, action, args, readHostOptions)
    : await desktop.browserPerformAction(viewId, action, args);
  if (!r?.ok) return { ok: false, error: r?.error ?? `${action} failed` };
  const data = actionData(r.result);
  return data ? { ok: true, data } : { ok: true };
}

export const SHOWN_ELSEWHERE_NOTE =
  "This tab's page is open in another Omnigent window, so the user sees it there, not in the window showing this session. Actions on it still work.";

/** For a tab whose page is in another desktop window, say so: this window
 *  can't show it, and the agent shouldn't tell the user it's on screen. */
async function whereShown(
  desktop: AgentDesktopBridge,
  sessionId: string,
  tabId: string,
): Promise<{ shown_in_other_window?: true; note?: string }> {
  const view = await desktop
    .browserHasView?.(browserViewId(sessionId, tabId))
    .catch(() => undefined);
  return view && !view.exists && view.elsewhere === true
    ? { shown_in_other_window: true, note: SHOWN_ELSEWHERE_NOTE }
    : {};
}

/** `browser_tabs`: list, open, select or close the session's browser tabs. */
async function manageTabs(
  sessionId: string,
  record: TabsRecord,
  args: Record<string, unknown>,
  desktop: AgentDesktopBridge,
  navigate: (viewId: string, url: string) => Promise<AgentActionResult>,
  deadlineAt: number,
  checkPage: (viewId: string, opts?: { select?: boolean }) => Promise<AgentActionResult>,
): Promise<AgentActionResult> {
  try {
    return await changeTabs(sessionId, record, args, desktop, navigate, deadlineAt, checkPage);
  } catch (error) {
    // A tab change not made by the deadline never happens.
    if (error instanceof BrowserTabsExpired) return { ok: false, error: error.message };
    throw error;
  }
}

async function changeTabs(
  sessionId: string,
  record: TabsRecord,
  args: Record<string, unknown>,
  desktop: AgentDesktopBridge,
  navigate: (viewId: string, url: string) => Promise<AgentActionResult>,
  deadlineAt: number,
  checkPage: (viewId: string, opts?: { select?: boolean }) => Promise<AgentActionResult>,
): Promise<AgentActionResult> {
  const tabId = typeof args.tab_id === "string" ? args.tab_id : undefined;
  const notOpen = (id: string) => ({
    ok: false,
    error: `no open tab ${id}; call browser_tabs with action list`,
  });
  switch (args.action) {
    case "list": {
      const current = agentTabIn(record);
      const tabs = await Promise.all(
        record.tabs.map(async (id) => {
          const viewId = browserViewId(sessionId, id);
          const hasView = () => desktop.browserHasView?.(viewId).catch(() => undefined);
          // A page this session's agent couldn't read stays unnamed, as for select.
          // The metadata is read after the check passes and again once it has,
          // so a page the view moved to in between is never named under the
          // earlier page's approval.
          const first = await hasView();
          let url = "";
          let title = "";
          if (first?.exists || first?.elsewhere === true) {
            const before = first.url;
            const checked = (await checkPage(viewId)).ok;
            const view = checked ? await hasView() : undefined;
            if (checked && view && view.url === before) {
              url = view.url ?? "";
              title = view.title ?? "";
            }
          }
          return { tab_id: id, url, title, current: id === current };
        }),
      );
      return { ok: true, data: { tabs, current_tab_id: current } };
    }
    case "new": {
      const url = typeof args.url === "string" ? args.url : "";
      const added = await addBrowserTab(sessionId, deadlineAt);
      if (!added) {
        return {
          ok: false,
          error: `this session already has ${BROWSER_TAB_LIMIT} tabs open; close one with browser_tabs close first`,
        };
      }
      if (url) {
        const viewId = browserViewId(sessionId, added.tabId);
        const opened = await navigate(viewId, url);
        // A refused first page leaves no tab behind, and the agent where it
        // was. Unless a newer navigation took the tab (the user typed an
        // address there meanwhile): its page is the user's now, so the tab
        // stays, just not as the agent's.
        if (!opened.ok) {
          if (!wasSuperseded(viewId, opened)) await discardNewBrowserTab(sessionId, added);
          return opened;
        }
      }
      // Past the deadline the tab stays open for the user, and the agent
      // stays on its tab.
      if (!(await chooseAgentBrowserTab(sessionId, added.tabId, { show: false, deadlineAt }))) {
        return { ok: false, error: `tab ${added.tabId} closed while it opened` };
      }
      return {
        ok: true,
        data: {
          tab_id: added.tabId,
          ...(url ? { url } : {}),
          ...(url ? await whereShown(desktop, sessionId, added.tabId) : {}),
        },
      };
    }
    case "select": {
      if (!tabId) return { ok: false, error: "tab_id is required to select a tab" };
      if (!record.tabs.includes(tabId)) return notOpen(tabId);
      // A user's tab can show a page this session's agent couldn't open
      // (a loopback or internal one): main checks it before the agent gets it.
      const viewId = browserViewId(sessionId, tabId);
      // A failed probe isn't a blank tab: it isn't chosen without its check.
      const view = await desktop.browserHasView?.(viewId).catch(() => null);
      if (view === null) return { ok: false, error: `couldn't check tab ${tabId}; try again` };
      if (view?.exists || view?.elsewhere === true) {
        const checked = await checkPage(viewId, { select: true });
        if (!checked.ok) return checked;
      }
      if (!(await chooseAgentBrowserTab(sessionId, tabId, { show: true, deadlineAt }))) {
        return notOpen(tabId);
      }
      return {
        ok: true,
        data: { tab_id: tabId, ...(await whereShown(desktop, sessionId, tabId)) },
      };
    }
    case "close": {
      const target = tabId ?? agentTabIn(record);
      if (!record.tabs.includes(target)) return notOpen(target);
      const closed = await closeBrowserTab(sessionId, record, target, deadlineAt);
      if (closed === "navigated") {
        return {
          ok: false,
          error: `tab ${target} went to a new page while it was closing, so it was left open; list the tabs to see it`,
        };
      }
      if (closed === "failed") return { ok: false, error: `tab ${target} did not close` };
      return {
        ok: true,
        data: { closed_tab_id: target, current_tab_id: agentBrowserTabId(sessionId) },
      };
    }
    default:
      return { ok: false, error: "action must be one of list, new, select, close" };
  }
}

/**
 * Run one claimed action for a session, on its current agent tab.
 *
 * Takes the relay's arguments plus its `dispatch`, which runs
 * navigate / snapshot / click / type / screenshot on the tab's page.
 * Desktop apps without the advanced tools run that dispatch as is.
 * Never throws: errors come back as `{ ok: false, error }`.
 */
export async function dispatchAgentBrowserAction<D extends object, R extends AgentActionResult>(
  dispatch: Dispatch<D, R>,
  sessionId: string,
  action: string,
  args: Record<string, unknown>,
  desktop: D,
  sourceHostId: string | null,
  rememberedHostId: string | null,
  sourceIsManagedHost: boolean,
  {
    deadlineAt = Date.now() + AGENT_ACTION_BUDGET_MS,
    readHostOptions,
  }: { deadlineAt?: number; readHostOptions?: BrowserHostOptions } = {},
): Promise<R | AgentActionResult> {
  // The relay's dispatch, given the deadline-bound bridge once the tools are in play.
  let on = desktop;
  const run = (id: string, name: string, runArgs: Record<string, unknown>) =>
    readHostOptions
      ? dispatch(
          id,
          name,
          runArgs,
          on,
          sourceHostId,
          rememberedHostId,
          sourceIsManagedHost,
          readHostOptions,
        )
      : dispatch(id, name, runArgs, on, sourceHostId, rememberedHostId, sourceIsManagedHost);
  try {
    if (!desktopSupportsBrowserAgentTools()) {
      const refusal = await olderDesktopError(sessionId, action, args, deadlineAt);
      return refusal ? { ok: false, error: refusal } : await run(sessionId, action, args);
    }
    // Whether the tools are on is read once: the switch can change while the
    // checks below wait, and the action's tab must not change with it.
    const active = browserAgentToolsActive();
    const offError = browserAgentToolsOffError(action, args);
    if (offError) return { ok: false, error: offError };
    const notOffered = await advancedToolsNotInSessionError(sessionId, action, args, deadlineAt);
    if (notOffered) return { ok: false, error: notOffered };
    // With the tools on, the session's tabs (and the one agents act on) are main's.
    const record = active ? await loadTabs(sessionId) : null;
    // Turned off while those were read: an advanced action doesn't run.
    const offSince = browserAgentToolsOffError(action, args);
    if (offSince) return { ok: false, error: offSince };
    // Counted from the claim: past it, the agent has been told this failed.
    if (Date.now() >= deadlineAt) return { ok: false, error: AGENT_ACTION_EXPIRED_ERROR };
    const viewId = browserViewId(sessionId, record ? agentTabIn(record) : AGENT_BROWSER_TAB_ID);
    on = boundedDesktop(desktop, deadlineAt);
    const bridge = on as AgentDesktopBridge;
    // Input goes out with the session's host for main's URL policy (a model
    // can't supply its own).
    const { provenance: _fromModel, ...modelArgs } = args;
    // Real text input only where the session was told to expect it; otherwise
    // browser_type keeps its page-script input and change events.
    const realInput = action === "type" && (await sessionTypesRealInput(sessionId, deadlineAt));
    const provenance = realInput ? { sourceHostId, realInput } : { sourceHostId };
    const sent = INPUT_ACTIONS.has(action) ? { ...modelArgs, provenance } : args;
    if (HISTORY_ACTIONS.has(action)) {
      // The tools take no arguments; anything else is the relay's provenance.
      if (Object.keys(args).length > 0) {
        return { ok: false, error: `browser_${action} takes no arguments` };
      }
      return await performDesktopAction(
        bridge,
        viewId,
        action,
        { provenance: { sourceHostId } },
        readHostOptions,
      );
    }
    if (DESKTOP_ACTIONS.has(action))
      return await performDesktopAction(bridge, viewId, action, sent, readHostOptions);
    if (action === "tabs") {
      if (!record) return { ok: false, error: "browser_tabs is turned off" };
      return await manageTabs(
        sessionId,
        record,
        args,
        bridge,
        async (id, url) => {
          if (Date.now() >= deadlineAt) return { ok: false, error: AGENT_ACTION_EXPIRED_ERROR };
          return run(id, "navigate", { url });
        },
        deadlineAt,
        (id, { select = false } = {}) =>
          performDesktopAction(
            bridge,
            id,
            "check_page",
            { provenance: { sourceHostId }, ...(select ? { select } : {}) },
            // Each tab's page is checked against this session's host, whatever
            // the probed view needs: another tab can be the host-local one.
            readHostOptions ??
              (sourceHostId
                ? { sessionHostProtocol: SESSION_HOST_PROTOCOL, sourceHostId }
                : undefined),
          ),
      );
    }
    if (action === "click") {
      // The standard click takes a target only; main does buttons and double clicks.
      const { button, double_click: doubleClick } = sent;
      if ((button !== undefined && button !== "left") || doubleClick === true) {
        return await performDesktopAction(bridge, viewId, "click", sent, readHostOptions);
      }
    }
    return await run(viewId, action, sent);
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}
