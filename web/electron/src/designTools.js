// Main's side of the Browser pane's design tools (Annotate and Draw). An open tool draws in
// an overlay: a transparent view just above the page's, showing a document the app opened
// for it, so the user's input lands on the overlay and the page can't see or reach it.

"use strict";

const { randomUUID } = require("node:crypto");
const path = require("node:path");
const { hitTestScript } = require("./designToolsPage.mjs");

/** An empty preload, so an overlay never inherits the shell's bridges. */
const NO_BRIDGE_PRELOAD = path.join(__dirname, "popup_preload.js");

// Its own isolated world id: one that collides with another isolated world (an extension's,
// say) would share that world's globals with it.
const DESIGN_TOOLS_WORLD_ID = 1789;

// windowOpen only treats an open whose frame name starts with this as an overlay's.
const FRAME_PREFIX = "omnigent-design-tools:";

const NOT_OPEN = "the design tool isn't open on this page";
const ZOOM_CHANGED = "the page's zoom changed";
const NO_ANSWER = "the page didn't answer";
const OFF_SCREEN = "nothing to capture there";
// Long enough for a busy page; a hung page mustn't stall the tool.
const OP_TIMEOUT_MS = 5000;
// How often an open tool checks that its overlay is still at the page's zoom (nothing
// reports a zoom set on the page itself).
const ZOOM_CHECK_MS = 250;
// A notice overlay's gap above the page's bottom edge, in page CSS pixels: with the 8px
// margin the app leaves round the notice, it sits 12px above the edge.
const NOTICE_GAP = 4;

/**
 * @typedef {object} DesignToolsSession
 * @property {string} id  The opening's id, which every request about it names.
 * @property {"tool" | "notice"} mode  A tool takes the user's input; a notice only shows.
 * @property {object} entry  The page's registry entry.
 * @property {object | undefined} owner  The app renderer's webContents that started it.
 * @property {object | null} host  The window its overlay is in, once opened.
 * @property {object | null} overlay  Its overlay view, once opened.
 * @property {{ width: number, height: number } | null} noticeSize  A notice's size.
 * @property {number | null} zoom  The page's zoom the overlay was last laid out at.
 * @property {{ width: number, height: number } | null} area  The page's part of its view
 *           while DevTools are docked there, in its CSS pixels; else null.
 * @property {boolean} claimed  Its overlay was asked for (it can be only once).
 * @property {boolean} [measuring]  Its page area is being measured.
 * @property {boolean} [refocus]  Its tool's overlay held the keys when its page left the window.
 * @property {() => void} detach
 * @property {(reason: string) => void} ended  End it, telling the app why.
 */

/** Requests whose points or rects are in the page's CSS pixels at the overlay's zoom. */
const POSITIONED_OPS = new Set(["capture", "hitTest", "scroll"]);

const isFiniteNumber = (value) => typeof value === "number" && Number.isFinite(value);

const WHEEL_MODIFIERS = new Set(["control", "meta", "shift", "alt"]);

/** The wheel's modifier keys, as sendInputEvent takes them (only the ones it can be). */
function asModifiers(value) {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.filter((key) => WHEEL_MODIFIERS.has(key)))];
}

/** A navigation event's main-frame and same-document flags, in either of Electron's forms. */
function navigation(details, isInPlace, isMainFrame) {
  return {
    mainFrame:
      (details && typeof details === "object" ? details.isMainFrame : undefined) ?? isMainFrame,
    sameDocument:
      (details && typeof details === "object" ? details.isSameDocument : undefined) ?? isInPlace,
  };
}

/**
 * Call `onGone` once when the page starts loading a new document in its main frame or
 * navigates within it (`"navigated"`), or crashes or is destroyed (`"gone"`).
 */
function watchPage(webContents, onGone) {
  let gone = false;
  const fire = (reason) => {
    if (gone) return;
    gone = true;
    detach();
    onGone(reason);
  };
  const navigated = () => fire("navigated");
  const listeners = [
    [
      "did-start-navigation",
      (details, _url, isInPlace, isMainFrame) => {
        const { mainFrame, sameDocument } = navigation(details, isInPlace, isMainFrame);
        if (mainFrame && !sameDocument) navigated();
      },
    ],
    [
      "did-navigate-in-page",
      (details, _url, isMainFrame) => {
        if (navigation(details, undefined, isMainFrame).mainFrame) navigated();
      },
    ],
    // A document that started loading before the tool opened ends it once it commits.
    ["did-navigate", navigated],
    ["render-process-gone", () => fire("gone")],
    ["destroyed", () => fire("gone")],
  ];
  function detach() {
    for (const [name, listener] of listeners) {
      try {
        webContents.removeListener(name, listener);
      } catch {
        /* destroyed */
      }
    }
  }
  for (const [name, listener] of listeners) webContents.on(name, listener);
  return { detach, gone: () => gone };
}

/**
 * Call `onGone` once when the app renderer `owner` (a webContents) loads a new document
 * in its main frame, crashes or is destroyed.
 */
function watchOwner(owner, onGone) {
  if (!owner || typeof owner.on !== "function") return { detach: () => {}, gone: () => false };
  let gone = owner.isDestroyed?.() === true;
  const fire = () => {
    if (gone) return;
    gone = true;
    detach();
    onGone();
  };
  const events = ["did-navigate", "render-process-gone", "destroyed"];
  function detach() {
    for (const name of events) {
      try {
        owner.removeListener(name, fire);
      } catch {
        /* destroyed */
      }
    }
  }
  if (!gone) for (const name of events) owner.on(name, fire);
  return { detach, gone: () => gone };
}

/** The part of `rect` inside the viewport (a crop past its edge would fail), or null if none. */
function clampRect(rect, viewport) {
  const x = Math.max(0, rect.x);
  const y = Math.max(0, rect.y);
  const fits = viewport.width > 0 && viewport.height > 0;
  const right = fits ? Math.min(viewport.width, rect.x + rect.width) : rect.x + rect.width;
  const bottom = fits ? Math.min(viewport.height, rect.y + rect.height) : rect.y + rect.height;
  if (right - x < 1 || bottom - y < 1) return null;
  return { x, y, width: right - x, height: bottom - y };
}

/** A CSS-pixel rect from the app, or null. */
function asRect(value) {
  if (!value || typeof value !== "object") return null;
  const { x, y, width, height } = /** @type {Record<string, unknown>} */ (value);
  if (![x, y, width, height].every(isFiniteNumber) || width <= 0 || height <= 0) return null;
  return { x, y, width, height };
}

/** A notice's size from the app (page CSS pixels), or null. */
function asSize(value) {
  if (!value || typeof value !== "object") return null;
  const { width, height } = /** @type {Record<string, unknown>} */ (value);
  if (![width, height].every(isFiniteNumber) || width <= 0 || height <= 0) return null;
  return { width: Math.min(width, 10_000), height: Math.min(height, 10_000) };
}

/** `promise`, or a rejection with NO_ANSWER after `ms`. */
function withTimeout(promise, ms) {
  let timer;
  const late = new Promise((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error(NO_ANSWER)), ms);
    timer.unref?.();
  });
  return Promise.race([promise, late]).finally(() => clearTimeout(timer));
}

/** Main's side of the design tools; tests inject `WebContentsView` and the timings. */
function createDesignTools({
  WebContentsView = null,
  opTimeoutMs = OP_TIMEOUT_MS,
  zoomCheckMs = ZOOM_CHECK_MS,
} = {}) {
  /** @type {WeakMap<object, DesignToolsSession>} each page entry's open opening */
  const views = new WeakMap();
  /** @type {Map<string, DesignToolsSession>} every open opening, by id */
  const sessions = new Map();

  const live = (webContents) => webContents && !webContents.isDestroyed?.();
  const zoomOf = (entry) => entry.view.webContents.getZoomFactor?.() || 1;
  /** The page's zoom has moved from the overlay's (laid out at another). */
  const zoomMoved = (session) =>
    session.overlay !== null && session.zoom !== null && zoomOf(session.entry) !== session.zoom;

  /** One page operation in the tools' world, answering within opTimeoutMs. */
  const inWorld = (webContents, code) =>
    withTimeout(
      Promise.resolve(
        webContents.executeJavaScriptInIsolatedWorld(DESIGN_TOOLS_WORLD_ID, [{ code }]),
      ),
      opTimeoutMs,
    );

  /** The page's own area in its view while DevTools are docked there (CSS pixels), else null. */
  async function pageArea(webContents) {
    if (!live(webContents) || !webContents.isDevToolsOpened?.()) return null;
    try {
      const [width, height] = JSON.parse(
        await inWorld(webContents, "JSON.stringify([innerWidth, innerHeight])"),
      );
      return width > 0 && height > 0 ? { width, height } : null;
    } catch {
      return null;
    }
  }

  /** Measure `session`'s page area again, and lay the overlay over it if it moved. */
  async function remeasure(session) {
    // A notice outlives its page's navigations: it never runs script in the next document.
    if (session.measuring || session.mode === "notice") return;
    session.measuring = true;
    try {
      const area = await pageArea(session.entry.view.webContents);
      if (sessions.get(session.id) !== session) return;
      const before = session.area;
      if (area?.width === before?.width && area?.height === before?.height) return;
      session.area = area;
      fit(session);
    } finally {
      session.measuring = false;
    }
  }

  // ── The overlay ──────────────────────────────────────────

  /**
   * Where `session`'s overlay goes in its window (DIPs), and whether it shows: a tool's
   * covers the page; a notice's is its own size, hidden until the app has measured it.
   */
  function overlayBounds(session) {
    const page = session.entry.view;
    const view = page.getBounds?.();
    if (!view || view.width < 1 || view.height < 1) return { bounds: null, visible: false };
    const visible = page.getVisible?.() !== false;
    const zoom = zoomOf(session.entry);
    // The page's part of its view: all of it, but for DevTools docked at its bottom.
    const bounds = session.area
      ? {
          ...view,
          width: Math.min(view.width, Math.round(session.area.width * zoom)),
          height: Math.min(view.height, Math.round(session.area.height * zoom)),
        }
      : view;
    if (session.mode === "tool") return { bounds, visible };
    if (!session.noticeSize) return { bounds, visible: false };
    const width = Math.min(bounds.width, Math.ceil(session.noticeSize.width * zoom));
    const height = Math.min(bounds.height, Math.ceil(session.noticeSize.height * zoom));
    return {
      bounds: {
        x: Math.round(bounds.x + (bounds.width - width) / 2),
        y: Math.round(bounds.y + bounds.height - NOTICE_GAP * zoom - height),
        width,
        height,
      },
      visible,
    };
  }

  /** Lay `session`'s overlay over its page again: bounds, zoom (the page's), shown or not. */
  function fit(session) {
    const overlay = session.overlay;
    if (!overlay || !live(overlay.webContents)) return;
    try {
      const zoom = zoomOf(session.entry);
      if (overlay.webContents.getZoomFactor?.() !== zoom) overlay.webContents.setZoomFactor(zoom);
      session.zoom = zoom;
      const { bounds, visible } = overlayBounds(session);
      if (bounds) overlay.setBounds(bounds);
      overlay.setVisible(bounds !== null && visible);
    } catch {
      /* destroyed */
    }
  }

  /** Put `session`'s overlay just above its page in the window, if the page is there. */
  function place(session) {
    const { host, overlay } = session;
    if (!host || !overlay || host.isDestroyed?.()) return;
    const { contentView } = host;
    if (contentView.children.includes(overlay)) contentView.removeChildView(overlay);
    const index = contentView.children.indexOf(session.entry.view);
    if (index < 0) return;
    // Just above the page, below whatever is above it (the reconnect overlay, prompts).
    contentView.addChildView(overlay, index + 1);
    fit(session);
    if (!session.refocus) return;
    session.refocus = false;
    if (session.mode === "tool" && live(overlay.webContents)) overlay.webContents.focus();
  }

  /**
   * Close the session's overlay. Its keys go back to the page when the user closed it; when
   * the page ended it, to the app, so a page can't end the tool to catch what's typed next.
   */
  function closeOverlay(session, { toPage = true } = {}) {
    const { host, overlay } = session;
    session.overlay = null;
    if (!overlay) return;
    let focused = false;
    try {
      focused = overlay.webContents.isFocused?.() === true;
    } catch {
      /* destroyed */
    }
    try {
      if (host && !host.isDestroyed?.()) host.contentView.removeChildView(overlay);
    } catch {
      /* window torn down */
    }
    try {
      if (live(overlay.webContents)) overlay.webContents.close();
    } catch {
      /* destroyed */
    }
    if (!focused) return;
    const page = session.entry.view.webContents;
    const shell = host && !host.isDestroyed?.() ? host.webContents : null;
    const next = toPage ? page : shell;
    if (live(next)) next.focus();
  }

  /**
   * The app renderer's `window.open` in `host`: the overlay of the opening its frame names,
   * if it may have one now, else denied. Null for any other window.open.
   */
  function windowOpen(host, details) {
    const frame = typeof details?.frameName === "string" ? details.frameName : "";
    if (!frame.startsWith(FRAME_PREFIX)) return null;
    const session = sessions.get(frame.slice(FRAME_PREFIX.length));
    const deny = { action: "deny" };
    if (!session || session.claimed || !WebContentsView) return deny;
    if (details.url !== "about:blank" || session.owner !== host.webContents) return deny;
    if (!host.contentView.children.includes(session.entry.view)) return deny;
    session.claimed = true;
    return {
      action: "allow",
      overrideBrowserWindowOptions: {
        webPreferences: {
          // The app's document, but none of the shell's bridges.
          preload: NO_BRIDGE_PRELOAD,
          sandbox: true,
          contextIsolation: true,
          nodeIntegration: false,
        },
      },
      createWindow: (options) => {
        const overlay = new WebContentsView({
          webContents: options.webContents,
          webPreferences: options.webPreferences,
        });
        const contents = overlay.webContents;
        overlay.setBackgroundColor("#00000000");
        contents.setWindowOpenHandler(() => ({ action: "deny" }));
        contents.on("will-navigate", (event) => event.preventDefault());
        if (sessions.get(session.id) !== session) {
          // Ended while it opened: nothing shows it.
          setImmediate(() => {
            if (live(contents)) contents.close();
          });
          return contents;
        }
        session.host = host;
        session.overlay = overlay;
        // Closed from the app's side, or gone: the opening ends with it.
        contents.once("destroyed", () => {
          if (session.overlay === overlay) session.ended("gone");
        });
        // The recent-session shortcut works from it as from the page (it takes the keys).
        session.entry.forwardRecentSessionInput?.(contents);
        // A tool takes the keys once it's over its page: now, or when its page is back.
        if (session.mode === "tool") session.refocus = true;
        place(session);
        return contents;
      },
    };
  }

  /** `view` (a page's) was added to `host`: its overlay goes back above it. */
  function viewAttached(host, view) {
    for (const session of sessions.values()) {
      if (session.host === host && session.entry.view === view) place(session);
    }
  }

  /** `view` (a page's) was removed from `host`: so is its overlay. */
  function viewDetached(host, view) {
    for (const session of sessions.values()) {
      if (session.host !== host || session.entry.view !== view || !session.overlay) continue;
      let focused = false;
      try {
        focused = session.overlay.webContents.isFocused?.() === true;
      } catch {
        /* destroyed */
      }
      try {
        host.contentView.removeChildView(session.overlay);
      } catch {
        /* window torn down */
      }
      // Hidden, it mustn't take what's typed: the app has the keys until it's back.
      if (!focused) continue;
      session.refocus = true;
      if (!host.isDestroyed?.() && live(host.webContents)) host.webContents.focus();
    }
  }

  /** The pages in `host` were shown or hidden in place (an app dialog over them). */
  function visibilityChanged(host) {
    for (const session of sessions.values()) if (session.host === host) fit(session);
  }

  // ── Openings ─────────────────────────────────────────────

  /**
   * Open a tool (or, with `input: false`, a notice) on a view, replacing whatever opening
   * it had: answers with the opening's id and the frame name its overlay opens as. `app.owner`
   * is the app renderer's webContents, whose reload or end ends the opening.
   */
  function start(viewId, entry, args, app) {
    // The app keeps the keys until the new overlay takes them.
    endSession(entry, undefined, { toPage: false });
    const id = randomUUID();
    /** @type {DesignToolsSession} */
    const session = {
      id,
      mode: args?.input === false ? "notice" : "tool",
      entry,
      owner: app.owner,
      host: null,
      overlay: null,
      noticeSize: null,
      zoom: null,
      area: null,
      claimed: false,
      detach: () => {},
      ended: () => {},
    };
    const emit = (payload) => app.send("browser-design-tools-event", { viewId, ...payload });
    session.ended = (reason) => {
      if (views.get(entry) !== session) return;
      endSession(entry, undefined, { toPage: false });
      emit({ type: "ended", session: id, reason });
    };
    // Nothing in an app that reloaded or went will stop this opening, so it ends at once,
    // and keys meant for the tool don't go on to the page.
    const owner = watchOwner(app.owner, () => endSession(entry, id, { toPage: false }));
    // A tool belongs to the page it opened on; a notice isn't the page's, but ends with it.
    const page =
      session.mode === "tool"
        ? watchPage(entry.view.webContents, (reason) => session.ended(reason))
        : null;
    const destroyed = () => session.ended("gone");
    if (!page) {
      entry.view.webContents.on?.("destroyed", destroyed);
      entry.view.webContents.on?.("render-process-gone", destroyed);
    }
    const bounds = () => fit(session);
    entry.view.on?.("bounds-changed", bounds);
    // DevTools opening or closing in the page's view moves the page's part of it.
    const devTools = () => void remeasure(session);
    entry.view.webContents.on?.("devtools-opened", devTools);
    entry.view.webContents.on?.("devtools-closed", devTools);
    // A zoom set on the page while it's open: its overlay follows.
    const zoomWatch = setInterval(() => {
      // A page destroyed under it ends it; its getters throw once it's gone.
      if (!live(entry.view.webContents)) return void session.ended("gone");
      try {
        if (zoomMoved(session)) fit(session);
      } catch {
        /* destroyed */
      }
      remeasure(session).catch(() => {});
    }, zoomCheckMs);
    zoomWatch.unref?.();
    void remeasure(session);
    session.detach = () => {
      owner.detach();
      page?.detach();
      clearInterval(zoomWatch);
      entry.view.removeListener?.("bounds-changed", bounds);
      entry.view.webContents.removeListener?.("devtools-opened", devTools);
      entry.view.webContents.removeListener?.("devtools-closed", devTools);
      entry.view.webContents.removeListener?.("destroyed", destroyed);
      entry.view.webContents.removeListener?.("render-process-gone", destroyed);
    };
    views.set(entry, session);
    sessions.set(id, session);
    if (owner.gone()) {
      endSession(entry, id, { toPage: false });
      return { ok: false, error: NOT_OPEN };
    }
    return { ok: true, session: id, frame: `${FRAME_PREFIX}${id}` };
  }

  /** End the view's opening, or only `id`'s when given, and close its overlay. */
  function endSession(entry, id, { toPage = true } = {}) {
    const session = entry ? views.get(entry) : undefined;
    if (!session || (id !== undefined && session.id !== id)) return false;
    views.delete(entry);
    sessions.delete(session.id);
    session.detach();
    closeOverlay(session, { toPage });
    return true;
  }

  /** Stop opening `id` (or whatever's open, without one). */
  function stop(entry, id) {
    return { ok: true, stopped: endSession(entry, typeof id === "string" ? id : undefined) };
  }

  // ── Requests ─────────────────────────────────────────────

  const invalid = () => ({ ok: false, error: "invalid design tools request" });
  const pageAnswer = (result) => ({ ok: true, result: typeof result === "string" ? result : "" });

  /** One request from the app about the opening `args.session`. */
  async function handle(entry, args) {
    const session = views.get(entry);
    const op = args?.op;
    if (!session || typeof args?.session !== "string" || args.session !== session.id) {
      return { ok: false, error: NOT_OPEN };
    }
    // A notice outlives its page's navigations, so it can't read or scroll the next document.
    if (session.mode === "notice" && op !== "fit") return { ok: false, error: NOT_OPEN };
    const webContents = entry.view.webContents;
    // An opening that ended while the page answered (it navigated, say) gets no answer.
    const ifOpen = (answer) =>
      views.get(entry) === session ? answer : { ok: false, error: NOT_OPEN };
    // Measured at the overlay's old zoom: the overlay follows the page's, and the app asks again.
    if (POSITIONED_OPS.has(op) && zoomMoved(session)) {
      fit(session);
      return { ok: false, error: ZOOM_CHANGED };
    }
    switch (op) {
      case "fit": {
        // The overlay's document resized (or a notice measured itself): lay it out again.
        if (session.mode === "notice") session.noticeSize = asSize(args.notice);
        fit(session);
        return { ok: true };
      }
      case "capture":
        return ifOpen(await capture(entry, args));
      case "hitTest": {
        if (!isFiniteNumber(args.x) || !isFiniteNumber(args.y)) return invalid();
        return ifOpen(pageAnswer(await inWorld(webContents, hitTestScript(args.x, args.y))));
      }
      case "scroll": {
        // The wheel turned over the tool: it scrolls what's under it, as the wheel would.
        const { x, y, dx, dy } = args;
        if (![x, y, dx, dy].every(isFiniteNumber)) return invalid();
        if (!live(webContents)) return { ok: false, error: NOT_OPEN };
        const zoom = zoomOf(entry);
        // With its modifiers, so Ctrl+wheel still zooms the page.
        const modifiers = asModifiers(args.modifiers);
        webContents.sendInputEvent({
          type: "mouseWheel",
          ...(modifiers.length > 0 ? { modifiers } : {}),
          x: Math.round(x * zoom),
          y: Math.round(y * zoom),
          deltaX: -dx * zoom,
          deltaY: -dy * zoom,
          canScroll: true,
        });
        return pageAnswer("");
      }
      default:
        return { ok: false, error: "unknown design tools operation" };
    }
  }

  /** The page alone (the overlay is another view), `args.rect` of it or all of it. */
  async function capture(entry, args) {
    const page = entry.view;
    // A view that isn't shown paints nothing (the command palette covers it, say).
    if (page.getVisible?.() === false) return { ok: false, error: "empty capture" };
    const zoom = zoomOf(entry);
    const bounds = page.getBounds?.() ?? { width: 0, height: 0 };
    // The page's viewport now, in its CSS pixels: its view, but for DevTools docked there.
    const viewport = (await pageArea(page.webContents)) ?? {
      width: bounds.width / zoom,
      height: bounds.height / zoom,
    };
    const wanted = asRect(args.rect);
    if (args.rect !== undefined && !wanted) return invalid();
    const rect = wanted && clampRect(wanted, viewport);
    if (wanted && !rect) return { ok: false, error: OFF_SCREEN };
    const image = await withTimeout(
      page.webContents.capturePage(
        rect
          ? {
              x: Math.round(rect.x * zoom),
              y: Math.round(rect.y * zoom),
              width: Math.max(1, Math.round(rect.width * zoom)),
              height: Math.max(1, Math.round(rect.height * zoom)),
            }
          : undefined,
      ),
      opTimeoutMs,
    );
    // A zoom while it measured or painted: the crop, viewport and image may disagree.
    if (zoomOf(entry) !== zoom) return { ok: false, error: ZOOM_CHANGED };
    const size = image.getSize();
    if (image.isEmpty() || size.width < 2 || size.height < 2) {
      return { ok: false, error: "empty capture" };
    }
    return {
      ok: true,
      png: image.toDataURL(),
      width: size.width,
      height: size.height,
      zoom,
      viewport,
    };
  }

  return {
    start,
    stop,
    handle,
    windowOpen,
    viewAttached,
    viewDetached,
    visibilityChanged,
    isOpen: (entry) => views.has(entry),
    /** The open tool's overlay on `entry`'s page holds the keys (the page itself doesn't then). */
    overlayFocused: (entry) => {
      const overlay = entry ? views.get(entry)?.overlay : null;
      try {
        return !!overlay && live(overlay.webContents) && overlay.webContents.isFocused() === true;
      } catch {
        return false;
      }
    },
  };
}

let shared = null;

/** The design tools main runs with (one for the app: openings name their view). */
function sharedDesignTools() {
  if (!shared) {
    const { WebContentsView } = require("electron");
    shared = createDesignTools({ WebContentsView });
  }
  return shared;
}

/** Whether `entry`'s page holds the keys: the page itself, or its open tool's overlay. */
function isBrowserViewFocused(entry) {
  // Before any tool opens there's no overlay to hold them, nor need to load Electron.
  return !!entry?.view.webContents.isFocused() || !!shared?.overlayFocused(entry);
}

/**
 * The `omnigent:browser-design-tools` channel: `start`, `stop`, and the open tool's
 * requests, for a view in the sender's window. `gateRegistry` is browserIpc's trust gate.
 */
function registerDesignToolsIpc({ ipcMain, gateRegistry, tools: given }) {
  // Resolved on first use, so registering doesn't load Electron (its IPC tests stub it).
  const designTools = () => given ?? sharedDesignTools();
  ipcMain.handle("omnigent:browser-design-tools", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const viewId = args?.conversationId;
    const entry = typeof viewId === "string" ? g.registry.get(viewId) : null;
    if (!entry) return { ok: false, error: "No browser view" };
    try {
      const tools = designTools();
      if (args.op === "start") {
        const app = {
          send: (channel, payload) => {
            try {
              event.sender.send(channel, payload);
            } catch {
              /* window torn down */
            }
          },
          owner: event.sender,
        };
        return tools.start(viewId, entry, args, app);
      }
      // Only the opening it names: a stop from a pane that's gone can't end a newer one.
      if (args.op === "stop") {
        if (typeof args.session !== "string") return { ok: false, error: "session is required" };
        return tools.stop(entry, args.session);
      }
      return await tools.handle(entry, args);
    } catch (error) {
      const message = error && error.message ? error.message : String(error);
      return { ok: false, error: message };
    }
  });
}

module.exports = {
  DESIGN_TOOLS_WORLD_ID,
  FRAME_PREFIX,
  createDesignTools,
  isBrowserViewFocused,
  registerDesignToolsIpc,
  sharedDesignTools,
};
