// IPC surface for the embedded browser pane, extracted out of main.js. main.js
// wires the per-window registry + trust gate and calls `registerBrowserIpc(...)`.
//
// Every handler is gated on `isPinnedOriginSender` and
// resolves the sender window's own registry, so one window can never drive
// another's panes. Do NOT drop the gate from any handler (toolbar ones included).

"use strict";

const crypto = require("node:crypto");
const { buildDesignModeScript } = require("./designModeScript");

// Max age of a real native input event for a design-mode submit marker to be
// honored (see the gesture gate below). Covers click/Enter → console.log.
const DESIGN_MODE_GESTURE_WINDOW_MS = 1500;

/**
 * Detach design-mode listeners (console-message + input-event) off an entry and
 * null them out. Safe when none attached; shared by enable-cleanup and disable.
 *
 * @param {object} entry registry entry
 */
function detachDesignModeListeners(entry) {
  if (!entry) return;
  const wc = entry.designModeWebContents;
  if (wc) {
    if (entry.designModeListener) {
      try {
        wc.removeListener("console-message", entry.designModeListener);
      } catch {
        /* destroyed */
      }
    }
    if (entry.designModeInputListener) {
      try {
        wc.removeListener("input-event", entry.designModeInputListener);
      } catch {
        /* destroyed */
      }
    }
  }
  entry.designModeListener = null;
  entry.designModeInputListener = null;
  entry.designModeWebContents = null;
}

/**
 * Read back/forward availability off a webContents. Prefers the Electron 42
 * `navigationHistory` API, falls back to the deprecated top-level methods for
 * older Electron. Never throws.
 *
 * @param {Electron.WebContents} wc
 * @returns {{ canGoBack: boolean, canGoForward: boolean }}
 */
function readNavState(wc) {
  try {
    const nav = wc.navigationHistory;
    if (nav && typeof nav.canGoBack === "function") {
      return { canGoBack: !!nav.canGoBack(), canGoForward: !!nav.canGoForward() };
    }
    if (typeof wc.canGoBack === "function") {
      return { canGoBack: !!wc.canGoBack(), canGoForward: !!wc.canGoForward() };
    }
  } catch {
    /* destroyed / mid-teardown */
  }
  return { canGoBack: false, canGoForward: false };
}

/** Navigate back through history, preferring the Electron 42 navigationHistory
 *  API. Returns true if a back navigation was issued. Never throws. */
function goBack(wc) {
  try {
    const nav = wc.navigationHistory;
    if (nav && typeof nav.canGoBack === "function") {
      if (nav.canGoBack()) {
        nav.goBack();
        return true;
      }
      return false;
    }
    if (typeof wc.canGoBack === "function" && wc.canGoBack()) {
      wc.goBack();
      return true;
    }
  } catch {
    /* destroyed */
  }
  return false;
}

/** Navigate forward through history. Returns true if issued. Never throws. */
function goForward(wc) {
  try {
    const nav = wc.navigationHistory;
    if (nav && typeof nav.canGoForward === "function") {
      if (nav.canGoForward()) {
        nav.goForward();
        return true;
      }
      return false;
    }
    if (typeof wc.canGoForward === "function" && wc.canGoForward()) {
      wc.goForward();
      return true;
    }
  } catch {
    /* destroyed */
  }
  return false;
}

/**
 * Wire nav listeners onto a new view so the URL bar live-tracks the real url
 * (redirects, in-page links, agent nav). Fires `browser-url-changed` +
 * `browser-nav-state` to the owning renderer. Attached once at create time.
 *
 * @param {object} params
 * @param {string} params.conversationId
 * @param {Electron.WebContents} params.webContents
 * @param {(channel: string, payload: unknown) => void} params.send  window-scoped sender
 */
function attachNavListeners({ conversationId, webContents, send }) {
  const emitUrl = (url) => {
    send("browser-url-changed", { conversationId, url });
    const { canGoBack, canGoForward } = readNavState(webContents);
    send("browser-nav-state", { conversationId, canGoBack, canGoForward });
  };
  // Full main-frame navigation (loadURL, redirects, back/forward, reload).
  webContents.on("did-navigate", (_e, url) => emitUrl(url));
  // SPA route changes / hash links / history.pushState within the same doc.
  webContents.on("did-navigate-in-page", (_e, url, isMainFrame) => {
    if (isMainFrame) emitUrl(url);
  });
}

// ── Design mode (point-and-prompt) ─────────────────────────────────────────
// A toolbar toggle injects an in-page picker: hover highlights, click opens an
// anchored input+Send popup, Send routes the element + a cropped screenshot to
// the agent via the normal chat path (no backend route — pure client affordance).
// The injected script (can't require electron) reports back over `console.log`
// markers, which the console listener below forwards to the owning renderer:
//   __omni_<nonce>_element_select__<json>         element clicked, popup shown
//   __omni_<nonce>_element_prompt_submit__<json>  user pressed Send / Enter
//   __omni_<nonce>_element_dismiss__              user pressed × / Escape
//
// SECURITY (console.log is a main-world back-channel a hostile top page could
// forge, so two layers):
//   1. Gesture gate (primary): a submit marker is honored only if a REAL native
//      input event landed in the view within DESIGN_MODE_GESTURE_WINDOW_MS —
//      page JS can't synthesize one, so unattended forged submits are dropped.
//   2. Nonce (defense-in-depth): every marker must echo a per-enable random
//      nonce; a cross-realm iframe can't read the top frame's console to learn it.

/**
 * Per-conversation design-mode console listener, bound to its webContents,
 * conversationId, the per-enable `nonce`, and a `gestureState` ref. A late
 * marker is tagged with its own conversationId and can't mutate another's
 * state. Stored on the registry entry so `close()` detaches it.
 *
 * @param {string} conversationId
 * @param {object} entry  registry entry (holds `.view`)
 * @param {(channel: string, payload: unknown) => void} send  window-scoped sender
 * @param {string} nonce  per-enable secret every legit marker must echo
 * @param {{ lastGestureAt: number }} gestureState  updated by the input-event listener
 * @returns {(event: unknown, level: unknown, message: unknown) => void}
 */
function makeDesignModeConsoleHandler(conversationId, entry, send, nonce, gestureState) {
  const SELECT = `__omni_${nonce}_element_select__`;
  const SUBMIT = `__omni_${nonce}_element_prompt_submit__`;
  const DISMISS = `__omni_${nonce}_element_dismiss__`;
  return (_event, _level, message) => {
    // The webContents may be destroyed mid-callback during teardown; bail
    // rather than fire against a dead object.
    if (!entry || !entry.view || entry.view.webContents.isDestroyed?.()) return;
    if (typeof message !== "string") return;
    // Nonce gate: any marker whose prefix doesn't carry THIS view's nonce is
    // ignored outright (stops cross-realm/iframe forgery).
    if (message.startsWith(SELECT)) {
      (async () => {
        try {
          const info = JSON.parse(message.slice(SELECT.length));
          let screenshotDataUrl = null;
          if (info.rect && info.rect.width > 0 && info.rect.height > 0) {
            const dpr = entry.view.webContents.getZoomFactor() || 1;
            const image = await entry.view.webContents.capturePage({
              x: Math.round(info.rect.x * dpr),
              y: Math.round(info.rect.y * dpr),
              width: Math.round(info.rect.width * dpr),
              height: Math.round(info.rect.height * dpr),
            });
            screenshotDataUrl = "data:image/png;base64," + image.toPNG().toString("base64");
          }
          send("browser-element-selected", {
            conversationId,
            ...info,
            screenshot: screenshotDataUrl,
          });
        } catch (e) {
          console.error("[design-mode]", e);
        }
      })();
      return;
    }
    if (message.startsWith(SUBMIT)) {
      // GESTURE GATE — the load-bearing check. A submit is an auto-send into
      // the agent, so it must be backed by a real, recent native gesture in
      // the view. No recent native input → drop silently (forged/unattended).
      const now = Date.now();
      const sinceGesture = now - (gestureState.lastGestureAt || 0);
      if (!gestureState.lastGestureAt || sinceGesture > DESIGN_MODE_GESTURE_WINDOW_MS) {
        return;
      }
      try {
        const payload = JSON.parse(message.slice(SUBMIT.length));
        send("browser-element-prompt-submit", { conversationId, ...payload });
      } catch (e) {
        console.error("[design-mode]", e);
      }
      return;
    }
    if (message === DISMISS) {
      send("browser-element-prompt-dismiss", { conversationId });
    }
  };
}

/**
 * Native input-event listener: stamps `gestureState.lastGestureAt` on a real
 * mouse/key-down inside the view. Page JS can't synthesize native input, so a
 * fresh stamp proves genuine interaction — the gesture gate the console handler
 * requires before honoring a submit marker.
 *
 * @param {{ lastGestureAt: number }} gestureState
 * @returns {(event: unknown, input: { type?: string }) => void}
 */
function makeDesignModeInputHandler(gestureState) {
  return (_event, input) => {
    const type = input && input.type;
    // mouseDown = click-to-select; keyDown = Enter-to-submit; rawKeyDown = pre-IME.
    if (type === "mouseDown" || type === "keyDown" || type === "rawKeyDown") {
      gestureState.lastGestureAt = Date.now();
    }
  };
}

/**
 * Register every `omnigent:browser-*` IPC handler. Idempotent per process is
 * NOT guaranteed — call exactly once from main.js's registerIpc.
 *
 * @param {object} deps
 * @param {Electron.IpcMain} deps.ipcMain
 * @param {(event: Electron.IpcMainInvokeEvent) => boolean} deps.isPinnedOriginSender
 *        The privileged-origin trust gate. Load-bearing — applied to every handler.
 * @param {(event: Electron.IpcMainInvokeEvent) =>
 *          (import('./browserViewRegistry').Registry | null)} deps.getRegistryForEvent
 *        Resolves the sender window's own browser-view registry.
 */
function registerBrowserIpc({ ipcMain, isPinnedOriginSender, getRegistryForEvent }) {
  /**
   * Resolve the sender's registry after the privileged-origin gate. Returns
   * `{ registry }` on success or `{ error }` (a structured result, never a
   * throw) so the relay/toolbar surfaces a clean error.
   */
  const gateRegistry = (event) => {
    if (!isPinnedOriginSender(event)) {
      return { error: "browser IPC is only available to the connected server's page" };
    }
    const registry = getRegistryForEvent(event);
    if (!registry) return { error: "no browser registry for this window" };
    return { registry };
  };

  /** A window-scoped sender for the event's own webContents. Used to push
   *  url/nav-state pings back to exactly the renderer that drives the view. */
  const senderFor = (event) => (channel, payload) => {
    try {
      event.sender.send(channel, payload);
    } catch {
      /* window torn down */
    }
  };

  // Open (create-if-absent) or navigate a conversation's view, and measure it
  // into place. `force` reloads even on the same URL (agent "bring me back"
  // intent). Returns the registry's structured `{ ok, created, error }`.
  ipcMain.handle("omnigent:browser-open-or-navigate", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId, url, bounds, opts } = args ?? {};
    if (typeof conversationId !== "string" || !conversationId) {
      return { ok: false, error: "conversationId is required" };
    }
    const r = g.registry.openOrNavigate(conversationId, url, bounds, opts);
    // On first creation, wire nav listeners here (not in the registry factory,
    // which stays Electron-free) so the URL bar can live-track the real url.
    if (r.ok && r.created && r.entry) {
      attachNavListeners({
        conversationId,
        webContents: r.entry.view.webContents,
        send: senderFor(event),
      });
    }
    // Strip the non-serializable `entry` before it crosses the IPC boundary.
    return { ok: r.ok, created: r.created ?? false, error: r.error };
  });

  // Attach the named conversation's view to the host window (detaching the
  // previous active one), or detach everything when conversationId is null.
  ipcMain.handle("omnigent:browser-set-active", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const conversationId = args?.conversationId ?? null;
    const r = g.registry.setActive(conversationId);
    return { ok: r.ok, error: r.error };
  });

  // Reposition the active conversation's view to freshly-measured bounds.
  ipcMain.handle("omnigent:browser-resize", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId, bounds } = args ?? {};
    if (typeof conversationId !== "string" || !conversationId) {
      return { ok: false, error: "conversationId is required" };
    }
    const entry = g.registry.get(conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    if (bounds) entry.boundsController.setRendererBounds(bounds);
    return { ok: true };
  });

  // Capture the conversation's view as a base64 PNG.
  ipcMain.handle("omnigent:browser-screenshot", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId } = args ?? {};
    const entry = g.registry.get(conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    try {
      const image = await entry.view.webContents.capturePage();
      const dataUrl = `data:image/png;base64,${image.toPNG().toString("base64")}`;
      return { ok: true, dataUrl };
    } catch (e) {
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
  });

  // Run relay-template JS in the conversation's view. PRIVATE to the relay's
  // fixed templates (snapshot / click / type) — NOT an agent-facing generic
  // `evaluate` (trust boundary; see README).
  ipcMain.handle("omnigent:browser-execute", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId, js } = args ?? {};
    if (typeof js !== "string") return { ok: false, error: "js must be a string" };
    const entry = g.registry.get(conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    try {
      // `true` = user gesture, so the page can call gesture-gated APIs.
      const result = await entry.view.webContents.executeJavaScript(js, true);
      // Normalize to a string — the relay JSON.parses snapshot/upload results.
      return { ok: true, result: typeof result === "string" ? result : JSON.stringify(result) };
    } catch (e) {
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
  });

  // Whether a view currently exists for a conversation. Lets a (re)mounting
  // pane re-attach an already-created view without waiting for a create event.
  ipcMain.handle("omnigent:browser-has-view", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { exists: false };
    const { conversationId } = args ?? {};
    return { exists: typeof conversationId === "string" && g.registry.has(conversationId) };
  });

  // Destroy the conversation's view (explicit close — unmount only detaches).
  ipcMain.handle("omnigent:browser-close", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId, reason } = args ?? {};
    const r = g.registry.close(conversationId, reason);
    return { ok: r.ok, removed: r.removed ?? false };
  });

  // ── Toolbar: history navigation ──────────────────────────────────────────
  // Back / forward / reload. Each returns fresh nav-state so the caller updates
  // button-disabled immediately without waiting for the did-navigate event.

  ipcMain.handle("omnigent:browser-go-back", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const entry = g.registry.get(args?.conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    goBack(entry.view.webContents);
    return { ok: true, ...readNavState(entry.view.webContents) };
  });

  ipcMain.handle("omnigent:browser-go-forward", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const entry = g.registry.get(args?.conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    goForward(entry.view.webContents);
    return { ok: true, ...readNavState(entry.view.webContents) };
  });

  ipcMain.handle("omnigent:browser-reload", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const entry = g.registry.get(args?.conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    try {
      entry.view.webContents.reload();
    } catch {
      /* destroyed */
    }
    return { ok: true };
  });

  // ── Toolbar: DevTools toggle ─────────────────────────────────────────────
  // Toggle DevTools docked 'bottom' — it shares the view's bounds, so the
  // syncBounds loop already covers it and Chromium splits page + devtools.
  ipcMain.handle("omnigent:open-browser-devtools", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const entry = g.registry.get(args?.conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    try {
      const wc = entry.view.webContents;
      if (wc.isDevToolsOpened()) {
        wc.closeDevTools();
      } else {
        wc.openDevTools({ mode: "bottom" });
      }
      return { ok: true };
    } catch (e) {
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
  });

  // ── Design mode (point-and-prompt) ───────────────────────────────────────
  // Enable/disable the in-page picker and signal a submit result back to the
  // popup. Listeners are stored per-entry (and detached by the registry's
  // close()) so a late background-conversation marker can't leak into another UI.

  ipcMain.handle("omnigent:browser-enable-design-mode", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId } = args ?? {};
    const entry = g.registry.get(conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    try {
      // Fresh per-enable nonce baked into the injected script's marker prefixes.
      const nonce = crypto.randomBytes(16).toString("hex");
      await entry.view.webContents.executeJavaScript(buildDesignModeScript(nonce));
      // Detach prior handlers so toggling on/off doesn't stack listeners.
      detachDesignModeListeners(entry);
      // Shared gesture state: input-event listener stamps the last native press;
      // the console handler requires a recent stamp before honoring a submit.
      const gestureState = { lastGestureAt: 0 };
      const consoleHandler = makeDesignModeConsoleHandler(
        conversationId,
        entry,
        senderFor(event),
        nonce,
        gestureState,
      );
      const inputHandler = makeDesignModeInputHandler(gestureState);
      entry.designModeListener = consoleHandler;
      entry.designModeInputListener = inputHandler;
      entry.designModeWebContents = entry.view.webContents;
      entry.designModeWebContents.on("console-message", consoleHandler);
      entry.designModeWebContents.on("input-event", inputHandler);
      return { ok: true };
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      if (msg.includes("Object has been destroyed")) return { ok: false, error: "browser closed" };
      return { ok: false, error: msg };
    }
  });

  ipcMain.handle("omnigent:browser-disable-design-mode", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId } = args ?? {};
    const entry = g.registry.get(conversationId);
    if (!entry) return { ok: false };
    detachDesignModeListeners(entry);
    try {
      await entry.view.webContents.executeJavaScript(
        "window.__omniDisableDesignMode && window.__omniDisableDesignMode()",
      );
    } catch {
      /* destroyed */
    }
    return { ok: true };
  });

  // Forward a submit's result envelope into the page for green/red feedback.
  // `id` matches the page's submitId so a late callback can't paint over a fresh
  // popup. Fields are defensively coerced before crossing back into the page.
  ipcMain.handle("omnigent:browser-signal-design-result", async (event, payload) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    if (!payload || typeof payload !== "object") return { ok: false, error: "bad payload" };
    const entry = g.registry.get(payload.conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    const safe = {
      id: typeof payload.id === "number" ? payload.id : 0,
      ok: !!payload.ok,
      message: typeof payload.message === "string" ? payload.message : "",
    };
    try {
      await entry.view.webContents.executeJavaScript(
        `window.__omniOnDesignResult && window.__omniOnDesignResult(${JSON.stringify(safe)})`,
      );
      return { ok: true };
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      if (msg.includes("Object has been destroyed")) return { ok: false, error: "browser closed" };
      return { ok: false, error: msg };
    }
  });
}

module.exports = {
  registerBrowserIpc,
  // Exported for unit tests (drive nav-state / listener logic without Electron).
  attachNavListeners,
  readNavState,
  goBack,
  goForward,
  // Design-mode security surface (nonce-gated console handler + native-gesture
  // tracker), exported so tests can drive the forge/gesture logic directly.
  makeDesignModeConsoleHandler,
  makeDesignModeInputHandler,
  buildDesignModeScript,
  DESIGN_MODE_GESTURE_WINDOW_MS,
};
