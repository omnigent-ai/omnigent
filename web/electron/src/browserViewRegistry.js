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

function createBrowserViewRegistry({
  WebContentsViewCtor, // (opts) => new WebContentsView(opts) — injectable for tests
  createBoundsController, // bounds-controller factory (createBrowserViewBoundsController)
  attachToHost, // (view) => mainWindow.contentView.addChildView(view)
  detachFromHost, // (view) => mainWindow.contentView.removeChildView(view)
  sendToRenderer, // (channel, payload) => mainWindow.webContents.send(...)
  getHostZoomFactor = () => 1,
  getHostDisplayScaleFactor = () => null,
  cap = DEFAULT_CAP,
} = {}) {
  const entries = new Map(); // conversationId -> BrowserViewEntry
  let activeConversationId = null;

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
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
      },
    });
    const entry = makeEntry(conversationId, view);
    entries.set(conversationId, entry);
    return { ok: true, entry, created: true };
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
    if (bounds) entry.boundsController.setRendererBounds(bounds);
    // Only attach immediately when this is the active conversation; otherwise
    // create-detached and let `setActive(conversationId)` attach on user switch.
    if (created && activeConversationId === conversationId) {
      try {
        attachToHost(entry.view);
      } catch {
        /* host gone */
      }
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
          } catch {}
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
          } catch {}
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
      } catch {}
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
    close,
    closeAll,
    // Introspection
    activeConversationId: () => activeConversationId,
    size: () => entries.size,
    has: (conversationId) => entries.has(conversationId),
    forEach: (fn) => entries.forEach(fn),
    // Constants exposed for tests / main.js wiring
    cap,
  };
}

module.exports = {
  createBrowserViewRegistry,
  DEFAULT_CAP,
};
