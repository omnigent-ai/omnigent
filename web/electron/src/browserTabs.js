// The agent browser tools' tab state, held here so every shell
// window agrees on it (web/src/components/BrowserPane/agentTools/tabStore.ts reads
// and writes it through the `browserTabs` bridge), and the one native page
// each of those tabs has across windows.
//
// A conversation has a record only while the renderer's switch is on, and
// only then is its tab ids' page single: an agent call for a page another
// window has runs there, and a user opening it in a second window is told
// where it is instead of getting a copy with its own storage.

"use strict";

const { viewTitle } = require("./agentBrowserActions");
const { pastAgentDeadline } = require("./agentDeadline");

// Bounds on what a renderer may store, so a bad record can't grow main.
const MAX_TABS = 100;
const MAX_ID_LENGTH = 200;

/** The session a view id belongs to (mirrors browserViewOwnerId in useBrowserTabs.ts). */
function viewConversationId(viewId) {
  const tab = String(viewId).match(/^browser-tab:([^:]+):[^:]+$/);
  if (!tab) return viewId;
  try {
    return decodeURIComponent(tab[1]);
  } catch {
    return viewId;
  }
}

const isId = (value) =>
  typeof value === "string" && value.length > 0 && value.length <= MAX_ID_LENGTH;

/** A record from the renderer, cleaned, or null when it isn't one. */
function cleanRecord(next) {
  if (!next || typeof next !== "object") return null;
  const { tabs, selected, agentTab, navs } = next;
  if (!Array.isArray(tabs) || tabs.length > MAX_TABS || !tabs.every(isId)) return null;
  if (new Set(tabs).size !== tabs.length) return null;
  if (selected !== null && !tabs.includes(selected)) return null;
  if (agentTab !== null && !isId(agentTab)) return null;
  const counts = {};
  if (navs !== undefined) {
    if (!navs || typeof navs !== "object" || Array.isArray(navs)) return null;
    const entries = Object.entries(navs);
    if (entries.length > MAX_TABS) return null;
    for (const [tabId, count] of entries) {
      if (!isId(tabId) || !Number.isSafeInteger(count) || count < 0) return null;
      counts[tabId] = count;
    }
  }
  return { tabs: [...tabs], selected, agentTab, navs: counts };
}

/**
 * @param {object} deps
 * @param {() => Iterable<[object, { origin?: string | null, browserRegistry?: object }]>} deps.windows
 *        The shell windows and their state (main.js's `windows`).
 * @param {(win: object) => boolean} deps.isDestroyed
 * @param {(win: object, channel: string, payload: unknown) => void} deps.send
 * @param {(state: { origin?: string | null, serverUrl?: string | null }) => string | null | undefined} [deps.serverOf]
 *        The server a window is on (default: its origin).
 */
function createBrowserTabs({ windows, isDestroyed, send, serverOf = (state) => state.origin }) {
  /** @type {Map<string, { version: number, tabs: string[], selected: string | null, agentTab: string | null, navs: Record<string, number> }>} */
  const records = new Map();

  // Windows opening a tab's page, from where the open was routed until it
  // ends: viewId -> (window -> opens in flight). Until its page exists, the
  // window opening it is where the tab's page is, so a second window can't
  // open another copy meanwhile and a close reaches the open (and cancels it).
  /** @type {Map<string, Map<object, number>>} */
  const opening = new Map();

  const live = () => [...windows()].filter(([win, state]) => !isDestroyed(win) && state.origin);
  // The server a window is on: same-origin mounts are different servers,
  // with their own sessions, so they share no records and no pages.
  const originOf = (win) => {
    const state = live().find(([w]) => w === win)?.[1];
    return state ? (serverOf(state) ?? null) : null;
  };
  const keyOf = (origin, conversationId) => `${origin}\u0000${conversationId}`;

  function broadcast(origin, conversationId, record) {
    for (const [win, state] of live()) {
      if (serverOf(state) === origin) {
        send(win, "omnigent:browser-tabs-changed", { conversationId, record });
      }
    }
  }

  /** The bridge's one call: `get`, `update` (compare-and-set) or `reset`. */
  function handle(senderWin, args) {
    const origin = originOf(senderWin);
    if (!origin) return { ok: false, error: "no shell window for this page" };
    const { op, conversationId } = args ?? {};
    if (op === "reset") {
      for (const key of [...records.keys()]) {
        if (key.startsWith(`${origin}\u0000`)) records.delete(key);
      }
      for (const [win, state] of live()) {
        if (serverOf(state) === origin) send(win, "omnigent:browser-tabs-changed", { reset: true });
      }
      return { ok: true };
    }
    if (!isId(conversationId)) return { ok: false, error: "conversationId is required" };
    const key = keyOf(origin, conversationId);
    const current = records.get(key) ?? null;
    if (op === "get") return { ok: true, record: current };
    if (op !== "update") return { ok: false, error: "unknown browser tabs operation" };
    // An agent's change past its deadline: the agent was told it failed.
    if (pastAgentDeadline(args)) return { ok: false, expired: true };
    if ((current?.version ?? 0) !== args.expectedVersion) {
      return { ok: false, conflict: true, record: current };
    }
    const cleaned = cleanRecord(args.next);
    if (!cleaned) return { ok: false, error: "invalid browser tabs record" };
    const record = { version: (current?.version ?? 0) + 1, ...cleaned };
    records.set(key, record);
    broadcast(origin, conversationId, record);
    return { ok: true, record };
  }

  const inWindow = ([win, state]) => ({
    win,
    registry: state.browserRegistry,
    send: (channel, payload) => send(win, channel, payload),
  });

  // Every copy of a page, for a close: closing a tab drops it for every
  // window, so a copy left in another window would live on unseen.
  const inEveryWindow = (owners) => ({
    registry: {
      get: (id) => owners[0][1].browserRegistry.get(id),
      close: (id, reason) => {
        const results = owners.map(([, state]) => state.browserRegistry.close(id, reason));
        return { ok: results.every((r) => r.ok), removed: results.some((r) => r.removed) };
      },
    },
    send: (channel, payload) => owners.forEach(([win]) => send(win, channel, payload)),
  });

  /**
   * Where a call about `viewId` from `senderWin` runs. Null leaves it to the
   * sender's own registry, as without the tools: the conversation has no record
   * (the switch is off), or the sender has the page. Otherwise:
   *   - more than one window has the page (copies from before the record):
   *     `{ duplicate: true }` for an agent call, which can't know which
   *     page it meant, every copy for a close, and the sender's own for a
   *     user's open;
   *   - another window has the page: `{ win, registry }` of that window for
   *     an agent call or a close (`{ registry, send }`), `{ elsewhere: true }` for a user's open;
   *   - no window has it: it opens where the session's other pages are,
   *     since each window has its own cookies and storage and a session's
   *     tabs share theirs. An agent call goes to that window (the agent
   *     tab's, if the session has pages in more than one, else refused as
   *     `{ split: true }`); a user's open in another window is
   *     `{ elsewhere: true, error }`. With no pages yet, the sender's own.
   *
   * @param {object | null} senderWin
   * @param {unknown} viewId
   * @param {"agent" | "user" | "close"} purpose
   */
  function route(senderWin, viewId, purpose) {
    // 1. Is this call ours to route at all? Without a record (switch off, or
    //    not a tab view) every window keeps its own pages, as without the tools.
    if (!senderWin || !isId(viewId)) return null;
    const origin = originOf(senderWin);
    const conversationId = viewConversationId(viewId);
    if (!origin || !records.has(keyOf(origin, conversationId))) return null;

    // 2. Who has this exact page? Only windows on the sender's server count
    //    (another server's sessions are unrelated). A window still opening the
    //    page counts as having it, so a second window can't race it to a copy.
    const peers = live().filter(([, state]) => serverOf(state) === origin && state.browserRegistry);
    const owners = peers.filter(
      ([win, state]) => state.browserRegistry.get(viewId) || opening.get(viewId)?.has(win),
    );

    // 3. Several copies (only possible from before the record existed). An
    //    agent can't say which one it means, so it's refused; a close must
    //    close all of them, or the tab would live on unseen in some window.
    //    A user's open falls through and uses the sender's own copy below.
    if (owners.length > 1 && purpose === "agent") return { duplicate: true };
    if (owners.length > 1 && purpose === "close") return inEveryWindow(owners);

    // 4. The sender has the page: nothing to route, it acts on its own.
    if (owners.some(([win]) => win === senderWin)) return null;

    // 5. Exactly one other window has it. Agent calls and closes run there,
    //    on the one real page. A user's open isn't moved across windows; the
    //    sender is told the page is elsewhere instead of making a copy.
    const owner = owners[0];
    if (owner) {
      if (purpose === "user") return { elsewhere: true };
      return inWindow(owner);
    }

    // 6. Nobody has the page yet, so it's about to be created. A close has
    //    nothing to close. For an open, pick the window it should live in:
    //    one that already has this session's other pages ("homes"), because
    //    each window has its own cookies and storage and a session's tabs
    //    must share them (a login in one tab works in the others).
    if (purpose === "close") return null;
    const homes = peers.filter(([win, state]) => hasSessionPage(win, state, conversationId));
    const here = ([win]) => win === senderWin;
    // The session's first page: it opens wherever it was asked for.
    if (homes.length === 0) return null;

    // 7. A user opening a new tab in the wrong window is told where the
    //    session's tabs are, rather than starting a second cookie jar.
    if (purpose === "user") {
      return homes.some(here) ? null : { elsewhere: true, error: SESSION_ELSEWHERE_ERROR };
    }

    // 8. An agent's open goes to the session's window. If its pages are
    //    already split across windows (from before the record), the agent
    //    tab's window wins; with no agent tab page to break the tie, there's
    //    no right answer, so the call is refused as split.
    let home = homes[0];
    if (homes.length > 1) {
      const record = records.get(keyOf(origin, conversationId));
      const agentView = record?.agentTab
        ? viewIdOf(conversationId, record.agentTab)
        : conversationId;
      home = homes.find(([, state]) => state.browserRegistry.get(agentView));
      if (!home) return { split: true };
    }
    return here(home) ? null : inWindow(home);
  }

  /** Whether `win` has, or is opening, a page of the session's. */
  function hasSessionPage(win, state, conversationId) {
    let found = false;
    state.browserRegistry.forEach?.((_entry, id) => {
      if (viewConversationId(id) === conversationId) found = true;
    });
    if (found) return true;
    for (const [id, holders] of opening) {
      if (holders.has(win) && viewConversationId(id) === conversationId) return true;
    }
    return false;
  }

  /**
   * Note `win` as opening `viewId` until the returned release is called.
   *
   * @param {object} win
   * @param {string} viewId
   */
  function holdOpen(win, viewId) {
    const holders = opening.get(viewId) ?? new Map();
    opening.set(viewId, holders);
    holders.set(win, (holders.get(win) ?? 0) + 1);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const left = (holders.get(win) ?? 1) - 1;
      if (left > 0) holders.set(win, left);
      else holders.delete(win);
      if (holders.size === 0 && opening.get(viewId) === holders) opening.delete(viewId);
    };
  }

  /**
   * browserIpc's open-or-navigate handler, with the window it is routed to
   * noted as opening the page until it ends (see `opening`).
   *
   * @param {(event: object) => object | null} windowForEvent
   * @param {Function} handler
   */
  function whileOpening(windowForEvent, handler) {
    return async (event, args) => {
      const senderWin = windowForEvent(event);
      const viewId = args?.conversationId;
      const purpose = args?.opts?.agent === true ? "agent" : "user";
      // The same routing the handler's gate does next, in the same turn.
      const routed = isId(viewId) ? route(senderWin, viewId, purpose) : undefined;
      const openIn = routed === null ? senderWin : routed?.win;
      const release = openIn ? holdOpen(openIn, viewId) : () => {};
      // Held until the open ends, or until an agent's deadline passes: past
      // that, browserIpc cancels the open, so a lookup that stalls past it
      // must not keep other windows out. An open with no deadline (the
      // user's) still creates its page when its lookup answers, so it holds
      // the tab until then.
      const deadlineAt = args?.opts?.deadlineAt;
      const timer = Number.isFinite(deadlineAt)
        ? setTimeout(release, Math.max(0, deadlineAt - Date.now()) + 1)
        : undefined;
      try {
        return await handler(event, args);
      } finally {
        clearTimeout(timer);
        release();
      }
    };
  }

  /**
   * Why an agent's close of a tab's page must not run, or null: its time ran
   * out, or the tab was navigated since the close read the record
   * (`expectedNavs` is `{ tabId, count }` from that record), so the page
   * there now is a newer intent than the close.
   *
   * @param {object | null} senderWin
   * @param {{ conversationId?: unknown, deadlineAt?: unknown, expectedNavs?: unknown }} args
   */
  function refuseClose(senderWin, args) {
    const refused = closeRefusal(senderWin, args);
    // A close that runs cancels any open of the page in flight (the
    // registry drops its navigation), so its hold ends here too.
    if (!refused && isId(args?.conversationId)) opening.delete(args.conversationId);
    return refused;
  }

  function closeRefusal(senderWin, args) {
    if (pastAgentDeadline(args)) return { ok: false, removed: false, expired: true };
    const viewId = args?.conversationId;
    const expected = args?.expectedNavs;
    if (!isId(viewId) || !expected || typeof expected !== "object") return null;
    const { tabId, count } = /** @type {{ tabId?: unknown, count?: unknown }} */ (expected);
    if (!isId(tabId) || !Number.isSafeInteger(count)) return null;
    const conversationId = viewConversationId(viewId);
    // The tab the view id is for: its own id, or the session's reserved tab.
    const ownTab = String(viewId).match(/^browser-tab:[^:]+:([^:]+)$/)?.[1];
    if (ownTab !== undefined ? ownTab !== tabId : viewId !== conversationId) return null;
    const origin = originOf(senderWin);
    const current = origin ? records.get(keyOf(origin, conversationId)) : undefined;
    if (!current || (current.navs[tabId] ?? 0) === count) return null;
    return { ok: false, removed: false, superseded: true };
  }

  return { handle, refuseClose, route, whileOpening };
}

/** A tab's view id (mirrors browserViewId in useBrowserTabs.ts; the
 *  session's reserved tab has the session's id). */
const viewIdOf = (conversationId, tabId) =>
  tabId === RESERVED_TAB_ID
    ? conversationId
    : `browser-tab:${encodeURIComponent(conversationId)}:${tabId}`;
const RESERVED_TAB_ID = "agent-browser";

const SESSION_ELSEWHERE_ERROR =
  "This session's browser tabs are open in another Omnigent window. Switch to that window to open more of them.";
const SPLIT_ERROR =
  "this session's browser tabs are open in more than one Omnigent window; close them in all but one, then try again";
const DUPLICATE_ERROR =
  "this tab's page is open in more than one Omnigent window; close the tab in all but one of them, then try again";
const ELSEWHERE_ERROR =
  "This tab's page is open in another Omnigent window. Switch to that window to use it.";

/**
 * browserIpc's registry gate with a tab's page routed to its window: the
 * sender's gate result when not routed, else the routed window's registry
 * and sender, or an error result for a duplicate or another window's page.
 *
 * @param {{ registry?: object, error?: string }} gate The sender's gate result.
 * @param {() => ({ registry: object, send: Function } | { duplicate: true } | { split: true } | { elsewhere: true, error?: string } | null)} route
 *        Called only once the sender passed the gate.
 * @param {Function} sendHere Sends to the sender's renderer.
 */
function routedGate(gate, route, sendHere) {
  if (gate.error) return gate;
  const routed = route();
  if (!routed) return { registry: gate.registry, send: sendHere };
  if ("duplicate" in routed) return { error: DUPLICATE_ERROR };
  if ("split" in routed) return { error: SPLIT_ERROR };
  if ("elsewhere" in routed) return { error: routed.error ?? ELSEWHERE_ERROR, elsewhere: true };
  return routed;
}

/**
 * The has-view answer for a page the sender doesn't have: where another
 * window has it, say so, with its URL and title (browser_tabs list).
 *
 * @param {{ registry?: object, error?: string }} gate `routedGate`'s result for a close.
 * @param {string} viewId
 */
function viewElsewhere(gate, viewId) {
  const entry = gate.error ? null : gate.registry?.get(viewId);
  if (!entry) return { exists: false };
  const { webContents } = entry.view;
  return {
    exists: false,
    elsewhere: true,
    url: webContents.getURL(),
    title: viewTitle(webContents),
  };
}

/**
 * main.js's one entry point into this module: creates the app-wide tab
 * records (one store for every shell window) and wires them to Electron.
 *
 * It does two things:
 *   - answers the renderer's `browserTabs` bridge on the
 *     `omnigent:browser-tabs` channel (`get`, `update`, `reset`; see
 *     `handle`), but only for the connected server's own page, so a page
 *     the user browses to in a tab can't read or rewrite the records;
 *   - returns the hooks browserIpc.js uses to send each browser call to the
 *     window that has the tab's page (see `route`), with Electron's IPC
 *     `event` in place of a window, since that is what browserIpc has.
 *
 * main.js calls this once at startup and passes the hooks it returns on to
 * registerBrowserIpc.
 *
 * @param {object} deps
 * @param {{ handle: (channel: string, listener: (event: object, args: unknown) => unknown) => void }} deps.ipcMain
 *        Electron's ipcMain; the bridge channel is registered on it.
 * @param {(event: object) => boolean} deps.isPinnedOriginSender
 *        Whether an IPC call comes from the connected server's page
 *        (main.js's check); anything else is refused.
 * @param {() => Map<object, { origin?: string | null, serverUrl?: string | null, browserRegistry?: object }>} deps.windows
 *        main.js's `windows` map (shell window -> its state), read on every
 *        call so windows opened or closed later are seen.
 * @param {(event: object) => object | null} deps.windowForEvent
 *        The shell window an IPC event came from
 *        (`BrowserWindow.fromWebContents(event.sender)`).
 * @param {(state: object) => string | null | undefined} [deps.serverOf]
 *        The server a window is on, which keys records and limits routing
 *        (two mounts on one origin are different servers). Defaults to the
 *        window's origin.
 * @param {(win: object) => boolean} [deps.showsServer]
 *        Whether the window's page is still the connected server's. A
 *        window that navigated elsewhere keeps its pinned origin and its
 *        pages, but receives none of the tab broadcasts or routed events.
 * @returns {{
 *   route: (event: object, viewId: unknown, purpose: "agent" | "user" | "close") => ReturnType<ReturnType<typeof createBrowserTabs>["route"]>,
 *   whileOpening: (handler: (event: object, args: any) => Promise<unknown>) => (event: object, args: any) => Promise<unknown>,
 *   refuseClose: (event: object, args: object) => { ok: false, removed: false, expired?: true, superseded?: true } | null,
 * }}
 *   - `route`: where a call about `viewId` from the event's window runs:
 *     null for the sender's own registry, the owning window's
 *     `{ registry, send }`, or a refusal (`duplicate`, `split`,
 *     `elsewhere`) for routedGate to turn into an error.
 *   - `whileOpening`: wraps browserIpc's open-or-navigate handler so the
 *     window an open was routed to holds the tab until the open ends,
 *     and no other window opens a copy meanwhile.
 *   - `refuseClose`: why an agent's close must not run (its deadline has
 *     passed, or the tab was navigated since), or null to let it close.
 */
function registerBrowserTabsIpc({
  ipcMain,
  isPinnedOriginSender,
  windows,
  windowForEvent,
  serverOf,
  showsServer = () => true,
}) {
  const tabs = createBrowserTabs({
    serverOf,
    windows: () => windows().entries(),
    isDestroyed: (win) => win.isDestroyed(),
    send: (win, channel, payload) => {
      try {
        if (!showsServer(win)) return;
        win.webContents.send(channel, payload);
      } catch {
        /* window torn down */
      }
    },
  });
  ipcMain.handle("omnigent:browser-tabs", (event, args) => {
    if (!isPinnedOriginSender(event)) {
      return { ok: false, error: "browser IPC is only available to the connected server's page" };
    }
    return tabs.handle(windowForEvent(event), args);
  });
  return {
    route: (event, viewId, purpose) => tabs.route(windowForEvent(event), viewId, purpose),
    whileOpening: (handler) => tabs.whileOpening(windowForEvent, handler),
    refuseClose: (event, args) => tabs.refuseClose(windowForEvent(event), args),
  };
}

module.exports = {
  cleanRecord,
  createBrowserTabs,
  registerBrowserTabsIpc,
  routedGate,
  viewConversationId,
  viewElsewhere,
};
