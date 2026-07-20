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
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
      },
    });
    const entry = makeEntry(conversationId, view);
    entries.set(conversationId, entry);
    denyChildWindowOpen(entry);
    attachAgentNavGuard(conversationId, entry);
    return { ok: true, entry, created: true };
  }

  // SECURITY: a visited page must not spawn windows from the desktop shell.
  // Deny every window.open / target=_blank on the child view (the safe default;
  // unlike the main shell window we do NOT route to shell.openExternal, since an
  // agent-visited page popping the user's real browser to an arbitrary URL is
  // itself an abuse vector).
  function denyChildWindowOpen(entry) {
    const wc = entry.view && entry.view.webContents;
    if (!wc || typeof wc.setWindowOpenHandler !== "function") return;
    wc.setWindowOpenHandler(() => ({ action: "deny" }));
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
