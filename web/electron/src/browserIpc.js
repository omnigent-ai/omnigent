// IPC surface for the embedded browser pane, extracted out of main.js. main.js
// wires the per-window registry + trust gate and calls `registerBrowserIpc(...)`.
//
// Every handler is gated on `isPinnedOriginSender` and
// resolves the sender window's own registry, so one window can never drive
// another's panes. Do NOT drop the gate from any handler (toolbar ones included).

"use strict";

const crypto = require("node:crypto");
const { buildBrowserActionScript } = require("./browserActionScript.mjs");
const chromeCookieStoreModule = require("./chromeCookieStore");
const { decodeLegacyBrowserAction } = require("./browserLegacyAction");
const { buildDesignModeScript } = require("./designModeScript");

const { SESSION_HOST_PROTOCOL } = require("./hostIdentity");

// Max age of a real native input event for a design-mode submit marker to be
// honored (see the gesture gate below). Covers click/Enter → console.log.
const DESIGN_MODE_GESTURE_WINDOW_MS = 1500;

// Chrome's cookie jar is OS-encrypted through the macOS Keychain; the other
// platforms' schemes aren't wired up yet (the shell's feature flag mirrors this).
const CHROME_COOKIE_IMPORT_PLATFORM_ERROR = "Importing Chrome cookies is only supported on macOS.";

/**
 * Profile names come from Chrome's `Local State`, which is just a file on disk.
 * Keep them one short line so a crafted name can't add its own paragraphs to
 * the consent dialog.
 *
 * @param {unknown} value
 * @param {string} fallback
 * @returns {string}
 */
function flattenProfileLabel(value, fallback) {
  let flattened = "";
  for (const char of String(value || fallback || "")) {
    const code = char.codePointAt(0);
    if (code < 0x20 || code === 0x7f) flattened += " ";
    // Quotes would close the prompt's own quoting around the name.
    else flattened += char === '"' ? "'" : char;
  }
  flattened = flattened.replace(/\s+/g, " ").trim();
  if (!flattened) return "Unknown";
  return flattened.length > 40 ? `${flattened.slice(0, 39)}…` : flattened;
}

/**
 * Consent text for the native prompt. Pure, so the wording and the flattening
 * of Chrome-supplied names are testable without Electron.
 *
 * @param {{ host: string, scope: string, profileName: string, cookieCount: number }} request
 * @returns {{ message: string, detail: string }}
 */
function buildCookieImportPrompt({ host, scope, profileName, cookieCount }) {
  const count = Number.isFinite(cookieCount) && cookieCount > 0 ? cookieCount : 0;
  const what = count > 0 ? `${count} cookie${count === 1 ? "" : "s"}` : "Chrome's cookies";
  // The scope is usually the tab's own host; name the tab too when an ancestor
  // was picked, so the prompt says which page the copy lands in.
  const where =
    host && host !== scope
      ? `${scope} and its subdomains (this tab is on ${host})`
      : `${scope} and its subdomains`;
  return {
    message: `Import ${what} into Omnigent?`,
    detail:
      `From Chrome profile "${flattenProfileLabel(profileName, scope)}" for ${where}.\n\n` +
      "They stay available to this conversation's embedded browser tabs, including any the " +
      "agent controls, until you quit Omnigent.",
  };
}

/**
 * Ask the user, from the main process, before Chrome's key is touched: the pane's
 * dialog is not proof a human asked, since the connected page holds the bridge.
 *
 * @param {{ host: string, scope: string, profileName: string, cookieCount: number,
 *           parentWindow?: object | null }} request
 * @returns {Promise<boolean>}
 */
async function askToImportChromeCookies({ host, scope, profileName, cookieCount, parentWindow }) {
  const { dialog } = require("electron");
  if (!dialog || typeof dialog.showMessageBox !== "function") return false;
  const options = {
    type: "warning",
    buttons: ["Cancel", "Import"],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
    ...buildCookieImportPrompt({ host, scope, profileName, cookieCount }),
  };
  // Parented like the shell's other consent dialogs: an unparented alert can be
  // approved while the window it describes is behind another app or on another
  // desktop.
  let parent = null;
  if (parentWindow && !parentWindow.isDestroyed?.()) parent = parentWindow;
  const { response } = parent
    ? await dialog.showMessageBox(parent, options)
    : await dialog.showMessageBox(options);
  return response === 1;
}

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
 * Track URL, title and history changes for the owning renderer, including
 * detached views. Attached once at create time.
 *
 * @param {object} params
 * @param {string} params.conversationId
 * @param {Electron.WebContents} params.webContents
 * @param {(channel: string, payload: unknown) => void} params.send  window-scoped sender
 */
function attachNavListeners({ conversationId, webContents, send }) {
  const emitPageMetadata = (url, title = webContents.getTitle()) => {
    send("browser-url-changed", { conversationId, url, title });
  };
  const emitUrl = (url) => {
    emitPageMetadata(url);
    const { canGoBack, canGoForward } = readNavState(webContents);
    send("browser-nav-state", { conversationId, canGoBack, canGoForward });
  };
  // Full main-frame navigation (loadURL, redirects, back/forward, reload).
  webContents.on("did-navigate", (_e, url) => emitUrl(url));
  // SPA route changes / hash links / history.pushState within the same doc.
  webContents.on("did-navigate-in-page", (_e, url, isMainFrame) => {
    if (isMainFrame) emitUrl(url);
  });
  webContents.on("page-title-updated", (_e, title) => {
    emitPageMetadata(webContents.getURL(), title);
  });
  webContents.on("did-finish-load", () => emitPageMetadata(webContents.getURL()));
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
 * @param {(event: Electron.IpcMainInvokeEvent) => (object | null)} [deps.getWindowForEvent]
 *        The shell window that owns the sender, used to parent native dialogs.
 */
function registerBrowserIpc({
  ipcMain,
  isPinnedOriginSender,
  getRegistryForEvent,
  getAgentContextForEvent = () => null,
  recoverAgentNavigationForEvent = async () => false,
  getAgentNavigationHintForEvent = () => null,
  // Injected so native dialogs can be parented to the sender's window.
  getWindowForEvent = () => null,
  chromeCookieStore = chromeCookieStoreModule,
  // Injected so the macOS-only guard is testable on any CI platform.
  platform = process.platform,
  // Injected so the native confirmation is testable without Electron dialogs.
  confirmCookieImport = askToImportChromeCookies,
}) {
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

  async function withAgentView(event, args) {
    const gated = gateRegistry(event);
    if (gated.error) return gated;
    const entry = gated.registry.get(args?.conversationId);
    if (!entry) return { error: "No browser view" };
    const context = entry.agentContext;
    if (context?.revoked) return { error: "The previous browser page is no longer available" };
    if (context?.hostIsThisMachine === true) {
      const opts = args?.opts;
      if (
        opts?.sessionHostProtocol !== SESSION_HOST_PROTOCOL ||
        typeof opts.sourceHostId !== "string" ||
        !opts.sourceHostId
      ) {
        return { error: "Current session host is required to read this browser view" };
      }
      const currentContext = getAgentContextForEvent(event, opts);
      if (
        opts.sourceHostId !== context.sourceHostId ||
        currentContext?.hostIsThisMachine !== true
      ) {
        await gated.registry.revokeAgentView(args.conversationId);
        return { error: "Session host changed; the previous browser page is no longer available" };
      }
    }
    return { entry };
  }

  // Open (create-if-absent) or navigate a conversation's view, and measure it
  // into place. `force` reloads even on the same URL (agent "bring me back"
  // intent). Returns the registry's structured `{ ok, created, error }`.
  ipcMain.handle("omnigent:browser-open-or-navigate", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId, url, bounds, opts } = args ?? {};
    if (typeof conversationId !== "string" || !conversationId) {
      return { ok: false, error: "conversationId is required" };
    }
    const isCurrentNavigation = g.registry.beginAgentNavigation(conversationId);
    const cancelled = { ok: false, created: false, error: "navigation superseded or closed" };
    const isCurrent = () => isCurrentNavigation() && isPinnedOriginSender(event);
    const navigate = async (registry) => {
      // Main validates the renderer-supplied host; opts only route.
      const agentContext = getAgentContextForEvent(event, opts);
      if (registry.needsAgentViewRevocation?.(registry.get(conversationId), agentContext)) {
        const cleared = await registry.revokeAgentView(conversationId, isCurrent);
        if (!isCurrent()) return cancelled;
        if (!cleared.ok) return cleared;
      }
      if (url) {
        const verdict = await registry.checkAgentNavigation(conversationId, url, agentContext);
        if (!isCurrent()) return cancelled;
        if (!verdict.ok) return verdict;
      }
      return registry.openOrNavigate(conversationId, url, bounds, {
        force: !!opts?.force,
        // Renderer flags do not establish user intent.
        agent: true,
        agentContext,
      });
    };
    let r = await navigate(g.registry);
    if (!isCurrent()) return cancelled;
    let recovered = false;
    if (!r.ok && opts?.agent) {
      try {
        recovered = await recoverAgentNavigationForEvent(
          event,
          url,
          opts?.sourceHostId,
          opts?.rememberedArcaHostId,
          opts?.sourceIsArcaHost,
        );
      } catch {
        recovered = false;
      }
    }
    if (!isCurrent()) return cancelled;
    if (recovered) {
      // Recovery updates main-owned identity, so rebuild the context and ask
      // the registry to evaluate the same navigation once more.
      const refreshed = gateRegistry(event);
      if (refreshed.error || refreshed.registry !== g.registry) return cancelled;
      r = await navigate(refreshed.registry);
      if (!isCurrent()) return cancelled;
    }
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
    const hint =
      !r.ok && opts?.agent
        ? getAgentNavigationHintForEvent(
            event,
            url,
            opts?.sourceHostId,
            opts?.rememberedArcaHostId,
            opts?.sourceIsArcaHost,
          )
        : null;
    return {
      ok: r.ok,
      created: r.created ?? false,
      ...(r.ok ? { hostIsThisMachine: r.entry?.agentContext?.hostIsThisMachine === true } : {}),
      error: hint ? `${r.error} ${hint}` : r.error,
    };
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

  // Hide/show the active view in place while a DOM overlay is open. The native
  // view always paints above the renderer, so z-index can't put a dialog/menu/
  // tooltip over it — the renderer ref-counts open overlays and toggles this.
  ipcMain.handle("omnigent:browser-set-suppressed", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const r = g.registry.setSuppressed(!!args?.suppressed);
    return { ok: r.ok, error: r.error };
  });

  // Intercept embedded-page Ctrl+Tab only while a compatible renderer is
  // subscribed; older server UIs must retain ownership of the shortcut.
  ipcMain.handle("omnigent:browser-set-recent-session-switch-supported", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    return g.registry.setRecentSessionSwitchSupported(!!args?.supported);
  });

  // A supported renderer can still decline the gesture when no sessions exist.
  ipcMain.handle("omnigent:browser-cancel-recent-session-switch", (event) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    return g.registry.cancelRecentSessionSwitch();
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
    const g = await withAgentView(event, args);
    if (g.error) return { ok: false, error: g.error };
    const { entry } = g;
    try {
      const image = await entry.view.webContents.capturePage();
      const dataUrl = `data:image/png;base64,${image.toPNG().toString("base64")}`;
      return { ok: true, dataUrl };
    } catch (e) {
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
  });

  // Both transports converge on main-owned scripts and the same sender gate.
  async function performBrowserAction(event, args, legacy = false) {
    const g = await withAgentView(event, args);
    if (g.error) return { ok: false, error: g.error };
    const { entry } = g;
    try {
      const { action, params } = legacy ? decodeLegacyBrowserAction(args?.js) : (args ?? {});
      const js = buildBrowserActionScript(action, params);
      const result = await entry.view.webContents.executeJavaScript(js, false);
      return { ok: true, result: typeof result === "string" ? result : JSON.stringify(result) };
    } catch (e) {
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
  }
  ipcMain.handle("omnigent:browser-perform-action", (event, args) =>
    performBrowserAction(event, args),
  );
  ipcMain.handle("omnigent:browser-execute", (event, args) =>
    performBrowserAction(event, args, true),
  );

  // Whether a view currently exists for a conversation. Lets a (re)mounting
  // pane re-attach an already-created view without waiting for a create event.
  ipcMain.handle("omnigent:browser-has-view", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { exists: false };
    const { conversationId } = args ?? {};
    const entry = typeof conversationId === "string" ? g.registry.get(conversationId) : null;
    if (!entry) return { exists: false };
    const webContents = entry.view.webContents;
    const url = webContents.getURL();
    // A new view has no committed URL while its first navigation is loading.
    const pendingUrl =
      (url === "" || url === "about:blank") &&
      (webContents.isLoadingMainFrame?.() ?? webContents.isLoading?.() ?? false)
        ? entry.lastRequestedUrl
        : undefined;
    return {
      exists: true,
      hostIsThisMachine: entry.agentContext?.hostIsThisMachine === true,
      url,
      title: webContents.getTitle(),
      ...(pendingUrl ? { pendingUrl } : {}),
      ...readNavState(webContents),
    };
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

  // ── Import cookies from Google Chrome (macOS) ────────────────────────────
  // Copies only the current site's cookies into this conversation's in-memory
  // partition: a metadata-only preview (no Keychain), then the import. The site
  // comes from the view's real URL in main and is re-checked before writing.

  const siteForEntry = (entry) => {
    let parsed;
    try {
      parsed = new URL(entry.view.webContents.getURL());
    } catch {
      return { error: "No website is loaded in this tab yet." };
    }
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
      return { error: "Importing cookies works on http(s) pages only." };
    }
    if (!parsed.hostname) return { error: "This page has no site to import cookies for." };
    return { host: parsed.hostname };
  };

  ipcMain.handle("omnigent:browser-chrome-cookie-preview", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    if (platform !== "darwin") {
      return { ok: false, error: CHROME_COOKIE_IMPORT_PLATFORM_ERROR };
    }
    const entry = g.registry.get(args?.conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    const site = siteForEntry(entry);
    if (site.error) return { ok: false, error: site.error };
    try {
      return await chromeCookieStore.previewSiteCookies({ host: site.host });
    } catch (e) {
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
  });

  ipcMain.handle("omnigent:browser-chrome-cookie-import", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    if (platform !== "darwin") {
      return { ok: false, error: CHROME_COOKIE_IMPORT_PLATFORM_ERROR };
    }
    const { conversationId, profileId, expectedHost, scope } = args ?? {};
    const entry = g.registry.get(conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    const site = siteForEntry(entry);
    if (site.error) return { ok: false, error: site.error };
    // Fall back to the real module only for stores that are pure stubs.
    const normalizeHost = chromeCookieStore.normalizeHost ?? chromeCookieStoreModule.normalizeHost;
    const safeNormalize = (value) => {
      try {
        return normalizeHost(value);
      } catch {
        return null;
      }
    };
    const hostMatches = (value) => {
      const normalized = safeNormalize(value);
      return normalized !== null && normalized === safeNormalize(site.host);
    };
    if (typeof expectedHost !== "string" || !hostMatches(expectedHost)) {
      return {
        ok: false,
        error: "This tab is no longer on that site. Reopen the import dialog to retry.",
      };
    }
    if (typeof profileId !== "string" || !profileId) {
      return { ok: false, error: "Pick a Chrome profile to import from." };
    }
    try {
      // Counts and scope for the confirmation come from main's own preview, so
      // a renderer string can't understate what the user approves.
      const preview = await chromeCookieStore.previewSiteCookies({ host: site.host });
      // Counts, name and scope for the confirmation all come from main's own
      // preview, so refuse before the prompt whenever it can't supply them.
      if (!preview?.ok) {
        return {
          ok: false,
          error: preview?.error || "This page's address isn't a site we can import cookies for.",
        };
      }
      const profile = (preview.profiles ?? []).find((candidate) => candidate.id === profileId);
      if (!profile) {
        return { ok: false, error: "That Chrome profile has no cookies for this site." };
      }
      const requestedScope = safeNormalize(scope) ?? site.host;
      const offered = (profile.scopes ?? []).find((candidate) => candidate.host === requestedScope);
      if (!offered) {
        return { ok: false, error: "That site scope isn't available for this tab." };
      }
      const approved = await confirmCookieImport({
        host: site.host,
        scope: offered.host,
        profileName: profile.name,
        cookieCount: offered.cookieCount,
        parentWindow: getWindowForEvent(event) ?? null,
      });
      if (!approved) return { ok: false, cancelled: true };
      return await chromeCookieStore.importSiteCookies({
        host: site.host,
        // Normalized and taken from the preview above; the store re-validates
        // it against the page host's own ancestors.
        scope: offered.host,
        profileId,
        cookiesApi: entry.view.webContents.session.cookies,
        // The tab can navigate while the Keychain prompt is up; bail before any
        // cookie is written so the import can't land on the new site.
        beforeWrite: () => {
          const current = siteForEntry(entry);
          return !current.error && hostMatches(current.host);
        },
      });
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      if (msg.includes("Object has been destroyed")) return { ok: false, error: "browser closed" };
      return { ok: false, error: msg };
    }
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
  // Exported for unit tests (prompt wording + dialog parenting without Electron).
  buildCookieImportPrompt,
  askToImportChromeCookies,
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
