/**
 * Per-conversation WebContentsView registry.
 *
 * Keyed by `conversationId` for Omnigent's session model. Each entry owns its
 * own bounds controller so per-conversation state never cross-contaminates.
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

const { isAgentNavigationAllowed } = require("./browserUrlPolicy");

const DEFAULT_CAP = 10;

/**
 * Storage partition for one conversation's browser view. Every view MUST get
 * its own partition: with none, Electron places the view on
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
 * `conversationId` is interpolated raw. Production ids are opaque 32-character
 * UUID hex strings; encode them if that contract ever loosens.
 *
 * @param {string} scope Registry-unique namespace (one per shell window).
 * @param {string} conversationId
 * @returns {string}
 */
function agentPartition(scope, conversationId) {
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
  // Desktop affordances for the pane's context menu; injected so the registry
  // stays Electron-free. No-op defaults keep tests and non-menu hosts simple.
  openUrlExternal = () => {}, // (url) => shell.openExternal(url)
  copyTextToClipboard = () => {}, // (text) => clipboard.writeText(text)
  showContextMenu = () => {}, // (items) => Menu.buildFromTemplate(items).popup(...)
  cap = DEFAULT_CAP,
  // Partition namespace for this registry's views — see agentPartition.
  // Injectable so tests can pin it; defaults to a per-instance unique value.
  partitionScope = `w${++registrySeq}`,
} = {}) {
  const entries = new Map(); // conversationId -> BrowserViewEntry
  let activeConversationId = null;
  // When true, the active view is hidden in place (setVisible(false)) so DOM
  // overlays (dialogs, menus, tooltips, toasts) aren't covered by the native
  // layer, which always paints above the renderer regardless of z-index. Sticky
  // across attaches: a view that becomes active while suppressed stays hidden.
  let overlaySuppressed = false;

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
      // Whether the CURRENT navigation was agent-initiated. Set on every
      // openOrNavigate from opts.agent; read by the will-navigate/will-redirect
      // guard so the allowlist is enforced on the agent's whole nav chain
      // (initial load + every redirect / meta-refresh / location.href) but NOT
      // on user-typed URL-bar browsing, which stays permissive. SECURITY: without
      // this, the allowlist only guards the first hop and a redirect to an
      // internal host slips through (SSRF via screenshot).
      agentNavLocked: false,
      // Design-mode listeners + webContents, set by browserIpc's enable handler
      // and cleared on disable/close (console-message forwarder + native-gesture
      // tracker). Null until design mode is enabled for this entry.
      designModeListener: null,
      designModeInputListener: null,
      designModeWebContents: null,
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
    return { ok: true, entry, created: true };
  }

  // SECURITY: a visited page must not spawn windows from the desktop shell, so
  // window.open / target=_blank still never creates a window here and is never
  // routed to shell.openExternal (an agent-visited page popping the user's real
  // browser to an arbitrary URL is itself an abuse vector). Instead an http(s)
  // target navigates the SAME view in place — nothing the page couldn't already
  // do with location.href — so a clicked link goes somewhere instead of dying
  // silently. The agent nav guard cannot see this hop (will-navigate skips
  // programmatic loadURL), so an agent-locked view is allowlist-checked here.
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
      if (entry.agentNavLocked) {
        const verdict = isAgentNavigationAllowed(url);
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

  // SECURITY (SSRF): enforce the agent-navigation allowlist on the child view's
  // OWN navigation events, not just the first loadURL. A server 302 / meta-
  // refresh / location.href during an agent-initiated navigation would otherwise
  // redirect the view to an internal host (metadata / loopback / RFC-1918) with
  // no re-check, and browser_screenshot could read it back. `will-navigate` and
  // `will-redirect` (plus subframes via `will-frame-navigate`) are the blocking
  // hooks — did-navigate is report-only and fires too late. Enforced ONLY while
  // `entry.agentNavLocked` (set per-navigation from opts.agent), so user-typed
  // URL-bar browsing — including legitimate auth-redirect chains to internal
  // hosts — stays permissive.
  function attachAgentNavGuard(conversationId, entry) {
    const wc = entry.view && entry.view.webContents;
    if (!wc || typeof wc.on !== "function") return;
    const guard = (event, targetUrl) => {
      if (!entry.agentNavLocked) return; // user-driven nav: permissive
      const verdict = isAgentNavigationAllowed(targetUrl);
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

  function openOrNavigate(conversationId, url, bounds, opts) {
    const force = !!(opts && opts.force);
    // Agent-driven nav (opts.agent) is gated by an allowlist (see
    // browserUrlPolicy) so the model can't point the view at file:// /
    // metadata / loopback / private hosts and exfiltrate via screenshot. URL-bar
    // (user-typed) nav stays permissive. Checked before getOrCreate so a
    // rejected nav creates no blank view.
    if (opts && opts.agent && url) {
      const verdict = isAgentNavigationAllowed(url);
      if (!verdict.ok) {
        return { ok: false, error: verdict.error };
      }
    }
    const result = getOrCreate(conversationId);
    if (!result.ok) return result;
    const { entry, created } = result;
    // Latch who drives THIS navigation so the will-navigate/will-redirect guard
    // enforces the allowlist on an agent nav's whole redirect chain, and leaves
    // user-typed URL-bar nav permissive. Set only when a url is actually issued.
    if (url) entry.agentNavLocked = !!(opts && opts.agent);
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
    const entry = entries.get(conversationId);
    if (!entry) return { ok: true, removed: false };
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
    for (const conversationId of [...entries.keys()]) {
      close(conversationId, reason);
    }
  }

  return {
    // Lifecycle
    get,
    getOrCreate,
    openOrNavigate,
    setActive,
    setSuppressed,
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
