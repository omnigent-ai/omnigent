/**
 * Per-conversation WebContentsView registry.
 *
 * Keyed by the agent's conversation ID or a user tab's view ID. Each entry owns
 * its bounds and navigation; tabs in one conversation share storage.
 *
 * Pure factory — no Electron imports at module scope. All deps are injected
 * so a unit test can drive create/swap/close/closeAll/cap behavior with a
 * stub `WebContentsViewCtor` without booting Electron.
 *
 * Lifecycle invariants:
 *  - `setActive` NEVER lazy-creates — it only attaches an existing entry (so a
 *    background agent's view isn't blanked by panel mounts). Creation goes only
 *    through `getOrCreate` / `openOrNavigate`, both cap-enforcing and non-throwing.
 *  - The old active entry is detached before the new one attaches. Inactive
 *    entries stay alive (JS + agent IPCs still run), just not painting; they're
 *    detached on hide and destroyed only on explicit close.
 */

const {
  checkAgentNavigation,
  isAgentConnectionAllowed,
  isAgentNavigationAllowed,
  isProxyAddress,
  lookupHostAddresses,
} = require("./browserUrlPolicy");

const DEFAULT_CAP = 10;

// Map WebSocket handshakes onto the HTTP URL rules.
function httpUrlForWebSocket(url) {
  return url.replace(/^ws(s?):/, "http$1:");
}

/**
 * Storage partition shared by one conversation's browser views. Without an
 * explicit partition, Electron places the view on
 * `session.defaultSession`, sharing one cookie/localStorage/cache store across
 * all agents and the main window (agent A's login bleeds into agent B's view).
 * Deliberately NOT `persist:`-prefixed — an in-memory partition keeps
 * agent-visited cookies off disk and leaves nothing to clean up when a
 * conversation is deleted, while Electron still reuses the same in-memory
 * session for the name within an app run (a reopened view keeps its login).
 *
 * `scope` namespaces the partition per REGISTRY (i.e. per shell window):
 * Electron sessions are app-global, but conversation ids are only unique per
 * server — two windows connected to different servers could carry the same
 * conversationId and would otherwise share a cookie jar.
 *
 * @param {string} scope Registry-unique namespace (one per shell window).
 * @param {string} viewId Conversation ID or encoded browser-tab view ID.
 * @returns {string}
 */
function agentPartition(scope, viewId) {
  let conversationId = viewId;
  // Session IDs are UUID hex strings; tab keys follow browserViewId in
  // web/src/hooks/useBrowserTabs.ts. Only storage uses the owning session ID.
  const tab = /^browser-tab:([^:]+):[^:]+$/.exec(viewId);
  if (tab) {
    try {
      conversationId = decodeURIComponent(tab[1]);
    } catch {
      // An invalid tab key keeps its own isolated partition.
    }
  }
  return `omnigent-agent-${scope}-${conversationId}`;
}

// Monotonic per-process counter: each registry (shell window) gets a distinct
// default partition scope. In-memory partitions never outlive the process, so
// no cross-run uniqueness is needed.
let registrySeq = 0;

function createBrowserViewRegistry({
  WebContentsViewCtor, // (opts) => new WebContentsView(opts) — injectable for tests
  createBoundsController, // bounds-controller factory (createBrowserViewBoundsController)
  attachToHost, // (view) => mainWindow.contentView.addChildView(view)
  detachFromHost, // (view) => mainWindow.contentView.removeChildView(view)
  sendToRenderer, // (channel, payload) => mainWindow.webContents.send(...)
  getHostZoomFactor = () => 1,
  getHostDisplayScaleFactor = () => null,
  isHostFocused = () => true,
  // Desktop affordances for the pane's context menu; injected so the registry
  // stays Electron-free. No-op defaults keep tests and non-menu hosts simple.
  openUrlExternal = () => {}, // (url) => shell.openExternal(url)
  copyTextToClipboard = () => {}, // (text) => clipboard.writeText(text)
  showContextMenu = () => {}, // (items) => Menu.buildFromTemplate(items).popup(...)
  onSuppressionChange = () => {}, // dismiss shell-owned UI when an overlay hides the pane
  allowsLocalhost: contextAllowsLocalhost = () => false,
  // (hostname, partition) => Promise<string[]> for the agent DNS check, and
  // (url, partition) => Promise<string> ("DIRECT" or a PAC-style proxy list).
  resolveHost = lookupHostAddresses,
  resolveProxy = async () => "DIRECT",
  cap = DEFAULT_CAP,
  // Partition namespace for this registry's views — see agentPartition.
  // Injectable so tests can pin it; defaults to a per-instance unique value.
  partitionScope = `w${++registrySeq}`,
} = {}) {
  const entries = new Map(); // conversationId -> BrowserViewEntry
  const pendingNavigations = new Map();

  function beginAgentNavigation(conversationId) {
    const generation = Symbol();
    pendingNavigations.set(conversationId, generation);
    return () => pendingNavigations.get(conversationId) === generation;
  }
  const allowsLocalhost = (context) => !context?.revoked && contextAllowsLocalhost(context);

  function needsAgentViewRevocation(entry, nextContext) {
    const context = entry?.agentContext;
    return (
      context?.hostIsThisMachine === true &&
      (context.revoked ||
        !nextContext?.hostIsThisMachine ||
        nextContext.sourceHostId !== context.sourceHostId)
    );
  }

  async function revokeAgentView(conversationId, isCurrent = beginAgentNavigation(conversationId)) {
    const entry = get(conversationId);
    const context = entry?.agentContext;
    if (!context) return { ok: true };
    context.revoked = true;
    try {
      await entry.view.webContents.loadURL("about:blank");
      if (!isCurrent() || get(conversationId) !== entry || entry.agentContext !== context) {
        return { ok: false, error: "navigation superseded or closed" };
      }
      entry.agentContext = null;
      entry.lastRequestedUrl = "about:blank";
      return { ok: true };
    } catch {
      return { ok: false, error: "Unable to clear the previous browser page" };
    }
  }
  let activeConversationId = null;
  // When true, the active view is hidden in place (setVisible(false)) so DOM
  // overlays (dialogs, menus, tooltips, toasts) aren't covered by the native
  // layer, which always paints above the renderer regardless of z-index. Sticky
  // across attaches: a view that becomes active while suppressed stays hidden.
  let overlaySuppressed = false;
  let recentSessionSwitchSupported = false;

  // Apply the current suppress flag to the active view (no-op with none active).
  function applyActiveVisibility() {
    if (activeConversationId === null) return;
    const entry = entries.get(activeConversationId);
    if (!entry) return;
    try {
      entry.view.setVisible(!overlaySuppressed);
    } catch {
      /* view destroyed */
    }
  }

  function setSuppressed(suppressed) {
    overlaySuppressed = !!suppressed;
    applyActiveVisibility();
    onSuppressionChange(overlaySuppressed);
    return { ok: true };
  }

  function makeEntry(conversationId, view) {
    const entry = {
      conversationId,
      view,
      boundsController: createBoundsController({
        getZoomFactor: getHostZoomFactor,
        getDisplayScaleFactor: getHostDisplayScaleFactor,
        setBounds: (bounds) => {
          // Only paint the active entry; inactive views are detached (no-op).
          if (activeConversationId === conversationId) {
            try {
              view.setBounds(bounds);
            } catch {
              /* destroyed */
            }
          }
        },
      }),
      // Last URL we EXPLICITLY requested (not getURL(), which drifts as the page
      // navigates) — lets openOrNavigate skip reissuing loadURL on a re-mount.
      lastRequestedUrl: "",
      // Enables navigation guards; renderer IPC always sets it.
      navigationPolicyEnforced: false,
      agentContext: null,
      // Design-mode listeners + webContents, set by browserIpc's enable handler
      // and cleared on disable/close (console-message forwarder + native-gesture
      // tracker). Null until design mode is enabled for this entry.
      designModeListener: null,
      designModeInputListener: null,
      designModeWebContents: null,
      recentSessionSwitching: false,
    };
    return entry;
  }

  function get(conversationId) {
    return entries.get(conversationId) || null;
  }

  function getOrCreate(conversationId) {
    const existing = entries.get(conversationId);
    if (existing) return { ok: true, entry: existing, created: false };
    if (entries.size >= cap) {
      return { ok: false, error: "browser view cap reached — close one", cap };
    }
    const view = WebContentsViewCtor({
      webPreferences: {
        // Per-conversation storage isolation — see agentPartition.
        partition: agentPartition(partitionScope, conversationId),
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
      },
    });
    const entry = makeEntry(conversationId, view);
    entries.set(conversationId, entry);
    installWindowOpenPolicy(entry);
    attachViewContextMenu(entry);
    attachAgentNavGuard(conversationId, entry);
    attachRecentSessionInput(entry);
    return { ok: true, entry, created: true };
  }

  // Popups stay in this view: an agent-visited page opening the user's system
  // browser is an abuse vector. Programmatic loadURL skips will-navigate,
  // so enforce navigation policy here too when enabled.
  function installWindowOpenPolicy(entry) {
    const wc = entry.view && entry.view.webContents;
    if (!wc || typeof wc.setWindowOpenHandler !== "function") return;
    wc.setWindowOpenHandler(({ url }) => {
      let parsed;
      try {
        parsed = new URL(url);
      } catch {
        return { action: "deny" }; // unparseable URL — nothing safe to open
      }
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
        return { action: "deny" };
      }
      if (entry.navigationPolicyEnforced) {
        const verdict = isAgentNavigationAllowed(url, {
          allowLocalhost: allowsLocalhost(entry.agentContext),
        });
        if (!verdict.ok) {
          sendToRenderer("browser-nav-blocked", {
            conversationId: entry.conversationId,
            url,
            error: verdict.error,
          });
          return { action: "deny" };
        }
      }
      try {
        wc.loadURL(url);
      } catch {
        /* view destroyed mid-click */
      }
      return { action: "deny" };
    });
  }

  // Right-click menu for the child view — the shell window's own menu covers
  // only the shell webContents, so without one a pane link can be neither
  // opened externally nor copied. "Open Link in Browser" is user-chosen, so
  // shell.openExternal is safe here (unlike the page-initiated path above).
  // Items are plain data; the host (main.js) builds the actual Electron Menu.
  function attachViewContextMenu(entry) {
    const wc = entry.view && entry.view.webContents;
    if (!wc || typeof wc.on !== "function") return;
    wc.on("context-menu", (_event, params) => {
      const items = [];
      if (params.linkURL) {
        if (/^https?:\/\//i.test(params.linkURL)) {
          items.push({
            label: "Open Link in Browser",
            click: () => openUrlExternal(params.linkURL),
          });
        }
        items.push({
          label: "Copy Link Address",
          click: () => copyTextToClipboard(params.linkURL),
        });
      }
      if (typeof params.selectionText === "string" && params.selectionText.trim() !== "") {
        if (items.length > 0) items.push({ type: "separator" });
        items.push({ label: "Copy", click: () => wc.copy() });
      }
      if (items.length === 0) return;
      showContextMenu(items);
    });
  }

  // Hook will-navigate/will-redirect: they can block; did-navigate fires too late.
  function attachAgentNavGuard(conversationId, entry) {
    const wc = entry.view && entry.view.webContents;
    if (!wc || typeof wc.on !== "function") return;
    const guard = (event, targetUrl) => {
      if (!entry.navigationPolicyEnforced) return;
      const verdict = isAgentNavigationAllowed(targetUrl, {
        allowLocalhost: allowsLocalhost(entry.agentContext),
      });
      if (!verdict.ok) {
        try {
          event.preventDefault();
        } catch {
          /* event shape without preventDefault — nothing to cancel */
        }
        sendToRenderer("browser-nav-blocked", {
          conversationId,
          url: targetUrl,
          error: verdict.error,
        });
      }
    };
    wc.on("will-navigate", guard);
    wc.on("will-redirect", guard);
    // Subframe navigations (iframes) can also reach an internal host; guard them
    // too. Older Electron may not emit this event — harmless if it never fires.
    wc.on("will-frame-navigate", (event) => {
      // will-frame-navigate passes a single event whose `.url` is the target.
      guard(event, event && event.url);
    });
  }

  // The embedded page owns a separate WebContents, so Ctrl+Tab never reaches
  // the shell renderer's window listener. Forward only the recent-session
  // gesture; all other page keyboard input remains local to the page.
  function cancelRecentSessionInput(entry, notifyRenderer) {
    if (!entry.recentSessionSwitching) return;
    entry.recentSessionSwitching = false;
    if (notifyRenderer) {
      sendToRenderer("browser-recent-session-input", {
        type: "keydown",
        key: "Escape",
        code: "Escape",
        ctrlKey: false,
        shiftKey: false,
        altKey: false,
        metaKey: false,
        repeat: false,
      });
    }
  }

  function cancelRecentSessionSwitch() {
    if (activeConversationId === null) return { ok: true };
    const entry = entries.get(activeConversationId);
    if (entry) cancelRecentSessionInput(entry, false);
    return { ok: true };
  }

  function setRecentSessionSwitchSupported(supported) {
    recentSessionSwitchSupported = !!supported;
    if (!recentSessionSwitchSupported) {
      entries.forEach((entry) => cancelRecentSessionInput(entry, false));
    }
    return { ok: true };
  }

  function attachRecentSessionInput(entry) {
    const wc = entry.view && entry.view.webContents;
    if (!wc || typeof wc.on !== "function") return;
    wc.on("before-input-event", (event, input) => {
      if (!recentSessionSwitchSupported) return;
      const type =
        input && input.type === "keyDown"
          ? "keydown"
          : input && input.type === "keyUp"
            ? "keyup"
            : null;
      if (type === null) return;
      const key = input.key || "";
      const startsSwitching =
        type === "keydown" && key === "Tab" && input.control && !input.alt && !input.meta;
      const commitsSwitching =
        type === "keyup" && key === "Control" && entry.recentSessionSwitching;
      const cancelsSwitching =
        type === "keydown" && key === "Escape" && entry.recentSessionSwitching;
      if (!startsSwitching && !commitsSwitching && !cancelsSwitching) return;

      if (startsSwitching || cancelsSwitching) event.preventDefault();
      sendToRenderer("browser-recent-session-input", {
        type,
        key,
        code: input.code || key,
        ctrlKey: !!input.control,
        shiftKey: !!input.shift,
        altKey: !!input.alt,
        metaKey: !!input.meta,
        repeat: !!input.isAutoRepeat,
      });
      if (startsSwitching) entry.recentSessionSwitching = true;
      else entry.recentSessionSwitching = false;
    });
    wc.on("blur", () => {
      cancelRecentSessionInput(entry, !isHostFocused());
    });
  }

  function checkAgentNavigationWithPolicy(url, { allowLocalhost, partition }) {
    return checkAgentNavigation(url, {
      allowLocalhost,
      resolveHost: (hostname) => resolveHost(hostname, partition),
    });
  }

  // The IPC first hop uses the same partition resolver as network preflight.
  function checkAgentNavigationFor(conversationId, url, agentContext) {
    return checkAgentNavigationWithPolicy(url, {
      allowLocalhost: allowsLocalhost(agentContext),
      partition: agentPartition(partitionScope, conversationId),
    });
  }

  function entryForWebContents(webContentsId) {
    for (const entry of entries.values()) {
      try {
        if (entry.view.webContents.id === webContentsId) return entry;
      } catch {
        continue; // destroyed
      }
    }
    return null;
  }

  function requestPolicyFor(webContentsId, partition) {
    const entry = entryForWebContents(webContentsId);
    if (entry) {
      return entry.navigationPolicyEnforced
        ? {
            allowLocalhost: allowsLocalhost(entry.agentContext),
            conversationId: entry.conversationId,
            partition: partition ?? agentPartition(partitionScope, entry.conversationId),
          }
        : null;
    }
    if (!partition) return null;
    // Workers may lack a registered view. Every partition owner must grant
    // loopback; orphaned workers and partitions with blank tabs are public-only.
    const owners = [...entries.values()].filter(
      (owner) => agentPartition(partitionScope, owner.conversationId) === partition,
    );
    return {
      allowLocalhost:
        owners.length > 0 &&
        owners.every(
          (owner) => owner.navigationPolicyEnforced && allowsLocalhost(owner.agentContext),
        ),
      partition,
    };
  }

  // Resolve named hosts in the owning partition before any request is sent.
  // IP literals and eligible exact-loopback hosts need only the URL check.
  async function shouldCancelAgentRequest({ webContentsId, url }, partition) {
    const policy = requestPolicyFor(webContentsId, partition);
    if (!policy) return false;
    const policyUrl = httpUrlForWebSocket(url);
    const verdict = await checkAgentNavigationWithPolicy(policyUrl, policy);
    if (verdict.ok) return false;
    if (policy.conversationId) {
      sendToRenderer("browser-nav-blocked", {
        conversationId: policy.conversationId,
        url,
        error: verdict.error,
      });
    }
    return true;
  }

  // Cache hits can omit the connected IP, so guarded HTTP requests bypass
  // cache reads with no-cache while preserving the page's existing directives.
  function agentRequestHeaders({ webContentsId, requestHeaders }, partition) {
    if (!requestPolicyFor(webContentsId, partition)) return {};
    const headers = {};
    const directives = new Map(); // lowercased → as sent, keeping e.g. no-store
    for (const [name, value] of Object.entries(requestHeaders ?? {})) {
      if (name.toLowerCase() !== "cache-control") {
        headers[name] = value;
        continue;
      }
      for (const directive of String(value).split(",")) {
        const trimmed = directive.trim();
        if (trimmed && !directives.has(trimmed.toLowerCase())) {
          directives.set(trimmed.toLowerCase(), trimmed);
        }
      }
    }
    if (!directives.has("no-cache")) directives.set("no-cache", "no-cache");
    return { requestHeaders: { ...headers, "Cache-Control": [...directives.values()].join(", ") } };
  }

  // IP-less WebSocket upgrades repeat the partition DNS check, not endpoint
  // verification; DNS may change between the check and connection.
  async function shouldCancelAgentResponse(
    { webContentsId, url, ip, resourceType, statusCode },
    partition,
  ) {
    const policy = requestPolicyFor(webContentsId, partition);
    if (!policy) return false;
    const policyUrl = httpUrlForWebSocket(url);
    const verdict =
      !ip && resourceType === "webSocket" && statusCode === 101
        ? await checkAgentNavigationWithPolicy(policyUrl, policy)
        : isAgentConnectionAllowed(policyUrl, ip, policy);
    if (verdict.ok) return false;
    // Through a proxy `ip` is the proxy's address; accept it only if it is one
    // (same-IP limit documented on isProxyAddress).
    const proxied =
      !!ip &&
      (await isProxyAddress(ip, await resolveProxy(policyUrl, policy.partition), (hostname) =>
        resolveHost(hostname, policy.partition),
      ));
    if (proxied) return false;
    if (policy.conversationId) {
      sendToRenderer("browser-nav-blocked", {
        conversationId: policy.conversationId,
        url,
        error: verdict.error,
      });
    }
    return true;
  }

  function openOrNavigate(conversationId, url, bounds, opts) {
    const force = !!(opts && opts.force);
    if (needsAgentViewRevocation(get(conversationId), opts?.agentContext)) {
      return { ok: false, error: "Previous browser page must be cleared before navigating" };
    }
    // Validate before creating a view.
    if (opts && opts.agent && url) {
      const verdict = isAgentNavigationAllowed(url, {
        allowLocalhost: allowsLocalhost(opts.agentContext),
      });
      if (!verdict.ok) {
        return { ok: false, error: verdict.error };
      }
    }
    const result = getOrCreate(conversationId);
    if (!result.ok) return result;
    const { entry, created } = result;
    // Latch policy for this URL's redirect chain; URL-less remounts keep the prior state.
    if (url) {
      entry.navigationPolicyEnforced = !!(opts && opts.agent);
      entry.agentContext = entry.navigationPolicyEnforced ? (opts.agentContext ?? null) : null;
    }
    if (bounds) entry.boundsController.setRendererBounds(bounds);
    // Only attach immediately when this is the active conversation; otherwise
    // create-detached and let `setActive(conversationId)` attach on user switch.
    if (created && activeConversationId === conversationId) {
      try {
        attachToHost(entry.view);
      } catch {
        /* host gone */
      }
      // Honor a live overlay suppression on a just-created active view.
      applyActiveVisibility();
    }
    // Signal the renderer a view now exists. On a fresh conversation the view is
    // created detached (no host-active-changed fires), so without this the pane
    // never mounts its placeholder or calls setActive to attach it.
    if (created) {
      sendToRenderer("browser-view-created", { conversationId });
    }
    if (url) {
      // Reissue loadURL on a fresh entry, a different requested URL, or `force`
      // (agent "bring me back"). Comparing lastRequestedUrl — not getURL(), which
      // drifts with in-page nav — stops a re-mount from refreshing to the initial URL.
      if (created || force || entry.lastRequestedUrl !== url) {
        entry.lastRequestedUrl = url;
        try {
          entry.view.webContents.loadURL(url);
        } catch (e) {
          return { ok: false, error: `loadURL failed: ${e && e.message ? e.message : e}` };
        }
      }
    }
    return { ok: true, entry, created };
  }

  function setActive(conversationId) {
    // null = "detach everything" sentinel (no pane mounted): stop painting over
    // the React layout, but keep the view so its agent can still drive it.
    if (conversationId === null || conversationId === undefined) {
      if (activeConversationId !== null) {
        const prev = entries.get(activeConversationId);
        if (prev) {
          cancelRecentSessionInput(prev, true);
          try {
            detachFromHost(prev.view);
          } catch {
            /* view already detached */
          }
        }
        activeConversationId = null;
        sendToRenderer("browser-host-active-changed", { conversationId: null });
      }
      return { ok: true };
    }
    const next = entries.get(conversationId);
    if (!next) {
      // No view for this conversation: still detach whatever was visible, else
      // switching A (has browser) → B (none) leaves A painted over B's page.
      if (activeConversationId !== null) {
        const prev = entries.get(activeConversationId);
        if (prev) {
          cancelRecentSessionInput(prev, true);
          try {
            detachFromHost(prev.view);
          } catch {
            /* view already detached */
          }
        }
        activeConversationId = null;
        sendToRenderer("browser-host-active-changed", { conversationId: null });
      }
      return { ok: false, error: "No browser view" };
    }
    if (activeConversationId === conversationId) {
      // Already active — repositioning bounds is a re-apply, not a swap.
      next.boundsController.resync();
      return { ok: true };
    }
    if (activeConversationId !== null) {
      const prev = entries.get(activeConversationId);
      if (prev) {
        cancelRecentSessionInput(prev, true);
        try {
          detachFromHost(prev.view);
        } catch {
          /* detached / destroyed */
        }
      }
    }
    activeConversationId = conversationId;
    try {
      attachToHost(next.view);
    } catch {
      /* host gone */
    }
    // A view attaching while an overlay is open must stay hidden (sticky flag).
    applyActiveVisibility();
    next.boundsController.resync();
    sendToRenderer("browser-host-active-changed", { conversationId });
    return { ok: true };
  }

  function close(conversationId, reason) {
    pendingNavigations.delete(conversationId);
    const entry = entries.get(conversationId);
    if (!entry) return { ok: true, removed: false };
    cancelRecentSessionInput(entry, true);
    if (activeConversationId === conversationId) {
      try {
        detachFromHost(entry.view);
      } catch {
        /* view already detached */
      }
      activeConversationId = null;
    }
    // Detach any design-mode listeners before closing the webContents, so a
    // closed view leaves nothing dangling. No-op if design mode was never on.
    if (entry.designModeWebContents) {
      if (entry.designModeListener) {
        try {
          entry.designModeWebContents.removeListener("console-message", entry.designModeListener);
        } catch {
          /* destroyed */
        }
      }
      if (entry.designModeInputListener) {
        try {
          entry.designModeWebContents.removeListener("input-event", entry.designModeInputListener);
        } catch {
          /* destroyed */
        }
      }
      entry.designModeListener = null;
      entry.designModeInputListener = null;
      entry.designModeWebContents = null;
    }
    entry.boundsController.clear();
    try {
      entry.view.webContents.close();
    } catch {
      /* already destroyed */
    }
    entries.delete(conversationId);
    sendToRenderer("browser-view-closed", { conversationId, reason: reason || null });
    return { ok: true, removed: true };
  }

  function closeAll(reason) {
    pendingNavigations.clear();
    for (const conversationId of [...entries.keys()]) {
      close(conversationId, reason);
    }
  }

  return {
    // Lifecycle
    get,
    getOrCreate,
    openOrNavigate,
    beginAgentNavigation,
    needsAgentViewRevocation,
    revokeAgentView,
    checkAgentNavigation: checkAgentNavigationFor,
    shouldCancelAgentRequest,
    agentRequestHeaders,
    shouldCancelAgentResponse,
    setActive,
    setSuppressed,
    cancelRecentSessionSwitch,
    setRecentSessionSwitchSupported,
    close,
    closeAll,
    // Introspection
    activeConversationId: () => activeConversationId,
    isSuppressed: () => overlaySuppressed,
    size: () => entries.size,
    has: (conversationId) => entries.has(conversationId),
    forEach: (fn) => entries.forEach(fn),
    // Constants exposed for tests / main.js wiring
    cap,
  };
}

module.exports = {
  createBrowserViewRegistry,
  agentPartition,
  DEFAULT_CAP,
};
