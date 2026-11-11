// IPC surface for the embedded browser pane, extracted out of main.js. main.js
// wires the per-window registry + trust gate and calls `registerBrowserIpc(...)`.
//
// Every handler is gated on `isPinnedOriginSender` and
// resolves the sender window's own registry, so one window can never drive
// another's panes. Do NOT drop the gate from any handler (toolbar ones included).

import crypto from "node:crypto";

import type { Rectangle } from "electron";

import { buildBrowserActionScript } from "./browserActionScript.ts";
import type { AgentContext } from "./browserViewRegistry.ts";
import * as chromeCookieStoreModule from "./chromeCookieStore.ts";
import type {
  CookiePreview,
  ImportSiteCookiesOptions,
  ImportSiteCookiesResult,
} from "./chromeCookieStore.ts";
import { decodeLegacyBrowserAction } from "./browserLegacyAction.ts";
import { buildDesignModeScript } from "./designModeScript.ts";
import { SESSION_HOST_PROTOCOL } from "./hostIdentity.ts";
import type { BrowserCallResult } from "./ipc/bridge.ts";
import type { PushChannels } from "./ipc/contract.ts";
import { handle, push, type IpcMainHandlePort, type PushTarget } from "./ipc/main.ts";

// Max age of a real native input event for a design-mode submit marker to be
// honored (see the gesture gate below). Covers click/Enter → console.log.
const DESIGN_MODE_GESTURE_WINDOW_MS = 1500;

// Chrome's cookie jar is OS-encrypted through the macOS Keychain; the other
// platforms' schemes aren't wired up yet (the shell's feature flag mirrors this).
const CHROME_COOKIE_IMPORT_PLATFORM_ERROR = "Importing Chrome cookies is only supported on macOS.";

/** Pushes this module sends to the renderer that drives a view. */
type BrowserPushChannel =
  | "browser-url-changed"
  | "browser-nav-state"
  | "browser-element-selected"
  | "browser-element-prompt-submit"
  | "browser-element-prompt-dismiss";

/** A window-scoped sender for the pushes above, typed by the contract. */
export type BrowserPushSender = <Channel extends BrowserPushChannel>(
  channel: Channel,
  ...args: PushChannels[Channel]["args"]
) => void;

/** Back/forward through a webContents, with the pre-Electron 42 methods as fallback. */
export interface HistoryWebContents {
  navigationHistory?: {
    canGoBack: () => boolean;
    canGoForward: () => boolean;
    goBack: () => void;
    goForward: () => void;
  };
  canGoBack: () => boolean;
  canGoForward: () => boolean;
  goBack: () => void;
  goForward: () => void;
}

/** The `input-event` fields the gesture tracker reads. */
export interface DesignModeInput {
  type?: string;
}

export type DesignModeConsoleListener = (event: unknown, level: unknown, message: unknown) => void;
export type DesignModeInputListener = (
  event: unknown,
  input: DesignModeInput | null | undefined,
) => void;

/** The webContents design mode attaches its listeners to. */
export interface DesignModeWebContents {
  on: ((event: "console-message", listener: DesignModeConsoleListener) => unknown) &
    ((event: "input-event", listener: DesignModeInputListener) => unknown);
  removeListener: ((event: "console-message", listener: DesignModeConsoleListener) => unknown) &
    ((event: "input-event", listener: DesignModeInputListener) => unknown);
}

/** The webContents events the URL bar tracks. */
export interface NavigationWebContents {
  getURL: () => string;
  getTitle: () => string;
  on: ((event: "did-navigate", listener: (event: unknown, url: string) => void) => unknown) &
    ((
      event: "did-navigate-in-page",
      listener: (event: unknown, url: string, isMainFrame: boolean) => void,
    ) => unknown) &
    ((event: "page-title-updated", listener: (event: unknown, title: string) => void) => unknown) &
    ((event: "did-finish-load", listener: () => void) => unknown);
}

/** The parts of a view's webContents the handlers drive. */
export interface BrowserIpcWebContents
  extends HistoryWebContents, DesignModeWebContents, NavigationWebContents {
  on: DesignModeWebContents["on"] & NavigationWebContents["on"];
  isDestroyed?: () => boolean;
  isLoadingMainFrame?: () => boolean;
  isLoading?: () => boolean;
  focus: () => void;
  isFocused: () => boolean;
  getZoomFactor: () => number;
  capturePage: (rect?: Rectangle) => Promise<{ toPNG: () => Buffer }>;
  executeJavaScript: (code: string, userGesture?: boolean) => Promise<unknown>;
  reload: () => void;
  isDevToolsOpened: () => boolean;
  closeDevTools: () => void;
  openDevTools: (options: { mode: "bottom" }) => void;
  session: { cookies: ImportSiteCookiesOptions["cookiesApi"] };
}

/** The parts of a registry entry the handlers drive. */
export interface BrowserIpcEntry {
  view: { webContents: BrowserIpcWebContents };
  // Handed the renderer's bounds unchecked (see BrowserIpcRegistry).
  boundsController: { setRendererBounds: (bounds: unknown) => unknown };
  lastRequestedUrl: string;
  agentContext: AgentContext | null;
  designModeListener: DesignModeConsoleListener | null;
  designModeInputListener: DesignModeInputListener | null;
  designModeWebContents: DesignModeWebContents | null;
}

/** A registry call's structured result. */
interface RegistryResult {
  ok: boolean;
  error?: string;
}

interface RegistryOpenResult extends RegistryResult {
  created?: boolean;
  entry?: BrowserIpcEntry;
}

/**
 * The parts of the per-window browser-view registry the handlers drive.
 * The real registry declares narrower parameters; the `unknown` ones are what
 * the handlers pass unchecked today, a deliberate mismatch until they validate.
 */
export interface BrowserIpcRegistry {
  get: (conversationId: unknown) => BrowserIpcEntry | null;
  beginAgentNavigation: (conversationId: string) => () => boolean;
  needsAgentViewRevocation?: (
    entry: BrowserIpcEntry | null,
    nextContext: AgentContext | null,
  ) => boolean;
  revokeAgentView: (conversationId: unknown, isCurrent?: () => boolean) => Promise<RegistryResult>;
  checkAgentNavigation: (
    conversationId: string,
    url: unknown,
    agentContext: AgentContext | null,
  ) => Promise<RegistryResult>;
  openOrNavigate: (
    conversationId: string,
    url: unknown,
    bounds: unknown,
    opts: { force: boolean; agent: true; agentContext: AgentContext | null },
  ) => RegistryOpenResult;
  setActive: (conversationId: unknown) => RegistryResult;
  setSuppressed: (suppressed: boolean) => RegistryResult;
  setRecentSessionSwitchSupported: (supported: boolean) => BrowserCallResult;
  setOpenTabSupported: (supported: boolean) => BrowserCallResult;
  cancelRecentSessionSwitch: () => BrowserCallResult;
  activeConversationId: () => string | null;
  close: (conversationId: unknown, reason: unknown) => { ok: boolean; removed?: boolean };
}

/** The Chrome cookie store calls the import handlers make. */
export interface BrowserIpcCookieStore {
  previewSiteCookies: (options: { host: string }) => Promise<CookiePreview>;
  importSiteCookies: (options: ImportSiteCookiesOptions) => Promise<ImportSiteCookiesResult>;
  /** Pure stubs may leave it out; the real module's is used then. */
  normalizeHost?: (host: unknown) => string;
}

/** A window a native dialog can be parented to. */
export interface DialogParentWindow {
  isDestroyed?: () => boolean;
}

export interface CookieImportMessageBoxOptions {
  type: "warning";
  buttons: string[];
  defaultId: number;
  cancelId: number;
  noLink: boolean;
  message: string;
  detail: string;
}

/** Electron's `dialog`, as far as the import confirmation goes. */
export interface CookieImportDialog {
  showMessageBox: ((
    parent: DialogParentWindow,
    options: CookieImportMessageBoxOptions,
  ) => Promise<{ response: number }>) &
    ((options: CookieImportMessageBoxOptions) => Promise<{ response: number }>);
}

export interface CookieImportPromptRequest {
  host: string;
  scope: string;
  profileName: string;
  cookieCount: number;
}

export interface CookieImportRequest extends CookieImportPromptRequest {
  parentWindow?: DialogParentWindow | null;
}

/** The parts of an invoke event the handlers use. */
export interface BrowserIpcEvent {
  sender: PushTarget;
}

export interface BrowserIpcDeps<Event extends BrowserIpcEvent> {
  ipcMain: IpcMainHandlePort<Event>;
  /** The privileged-origin trust gate. Load-bearing: applied to every handler. */
  isPinnedOriginSender: (event: Event) => boolean;
  /** Resolves the sender window's own browser-view registry. */
  getRegistryForEvent: (event: Event) => BrowserIpcRegistry | null;
  /** Main's agent context for the page's (unchecked) host options. */
  getAgentContextForEvent?: (event: Event, opts: unknown) => AgentContext | null;
  recoverAgentNavigationForEvent?: (
    event: Event,
    url: unknown,
    sourceHostId: unknown,
    rememberedArcaHostId: unknown,
    sourceIsArcaHost: unknown,
  ) => Promise<boolean>;
  getAgentNavigationHintForEvent?: (
    event: Event,
    url: unknown,
    sourceHostId: unknown,
    rememberedArcaHostId: unknown,
    sourceIsArcaHost: unknown,
  ) => string | null;
  /** The shell window that owns the sender, used to parent native dialogs. */
  getWindowForEvent?: (event: Event) => DialogParentWindow | null;
  chromeCookieStore?: BrowserIpcCookieStore;
  /** Injected so the macOS-only guard is testable on any CI platform. */
  platform?: string;
  /** Electron's `dialog`, for the default import confirmation. */
  dialog?: CookieImportDialog;
  /** Injected so the native confirmation is testable without Electron dialogs. */
  confirmCookieImport?: (request: CookieImportRequest) => Promise<boolean>;
}

/**
 * `value[key]` on an untrusted value; throws on null or undefined, as the
 * plain property read does.
 */
function readProperty(value: unknown, key: string): unknown {
  // Any value can be read this way; the cast only lets TypeScript index it.
  return (value as Record<string, unknown>)[key];
}

/** `value?.[key]` on an untrusted value. */
function optionalProperty(value: unknown, key: string): unknown {
  return value === null || value === undefined ? undefined : readProperty(value, key);
}

/** `e && e.message ? e.message : String(e)`, the handlers' error text. */
function errorText(error: unknown): string {
  const message = error ? readProperty(error, "message") : undefined;
  // A truthy non-string `message` is returned unchanged; the cast only satisfies the type.
  return message ? (message as string) : String(error);
}

/**
 * Profile names come from Chrome's `Local State`, which is just a file on disk.
 * Keep them one short line so a crafted name can't add its own paragraphs to
 * the consent dialog.
 */
function flattenProfileLabel(value: unknown, fallback: string): string {
  let flattened = "";
  for (const char of String(value || fallback || "")) {
    const code = char.codePointAt(0)!;
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
 */
function buildCookieImportPrompt({
  host,
  scope,
  profileName,
  cookieCount,
}: CookieImportPromptRequest): { message: string; detail: string } {
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
 */
async function askToImportChromeCookies(
  { host, scope, profileName, cookieCount, parentWindow }: CookieImportRequest,
  dialog: CookieImportDialog | undefined,
): Promise<boolean> {
  if (!dialog || typeof dialog.showMessageBox !== "function") return false;
  const options: CookieImportMessageBoxOptions = {
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
  let parent: DialogParentWindow | null = null;
  if (parentWindow && !parentWindow.isDestroyed?.()) parent = parentWindow;
  const { response } = parent
    ? await dialog.showMessageBox(parent, options)
    : await dialog.showMessageBox(options);
  return response === 1;
}

/**
 * Detach design-mode listeners (console-message + input-event) off an entry and
 * null them out. Safe when none attached; shared by enable-cleanup and disable.
 */
function detachDesignModeListeners(entry: BrowserIpcEntry | null | undefined): void {
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
 */
function readNavState(wc: HistoryWebContents): { canGoBack: boolean; canGoForward: boolean } {
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
function goBack(wc: HistoryWebContents): boolean {
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
function goForward(wc: HistoryWebContents): boolean {
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
 */
function attachNavListeners({
  conversationId,
  webContents,
  send,
}: {
  conversationId: string;
  webContents: NavigationWebContents & HistoryWebContents;
  /** Window-scoped sender. */
  send: BrowserPushSender;
}): void {
  const emitPageMetadata = (url: string, title = webContents.getTitle()) => {
    send("browser-url-changed", { conversationId, url, title });
  };
  const emitUrl = (url: string) => {
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

/** Updated by the input-event listener; read by the console handler. */
export interface GestureState {
  lastGestureAt: number;
}

/** The parts of an entry the console handler reads. */
export interface DesignModeEntry {
  view: {
    webContents: Pick<BrowserIpcWebContents, "isDestroyed" | "getZoomFactor" | "capturePage">;
  };
}

/**
 * Per-conversation design-mode console listener, bound to its webContents,
 * conversationId, the per-enable `nonce`, and a `gestureState` ref. A late
 * marker is tagged with its own conversationId and can't mutate another's
 * state. Stored on the registry entry so `close()` detaches it.
 *
 * `conversationId` is the enable request's, unchecked; marker JSON is spread
 * after it, so the page can replace it in the pushed payload.
 */
function makeDesignModeConsoleHandler(
  conversationId: unknown,
  entry: DesignModeEntry | null | undefined,
  send: BrowserPushSender,
  nonce: string,
  gestureState: GestureState,
): DesignModeConsoleListener {
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
          const info: unknown = JSON.parse(message.slice(SELECT.length));
          let screenshotDataUrl: string | null = null;
          // JSON values compare and multiply as their Number() would.
          const rect = readProperty(info, "rect");
          if (
            rect &&
            Number(readProperty(rect, "width")) > 0 &&
            Number(readProperty(rect, "height")) > 0
          ) {
            const dpr = entry.view.webContents.getZoomFactor() || 1;
            const image = await entry.view.webContents.capturePage({
              x: Math.round(Number(readProperty(rect, "x")) * dpr),
              y: Math.round(Number(readProperty(rect, "y")) * dpr),
              width: Math.round(Number(readProperty(rect, "width")) * dpr),
              height: Math.round(Number(readProperty(rect, "height")) * dpr),
            });
            screenshotDataUrl = "data:image/png;base64," + image.toPNG().toString("base64");
          }
          send("browser-element-selected", {
            conversationId,
            ...(info ?? {}),
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
        const payload: unknown = JSON.parse(message.slice(SUBMIT.length));
        send("browser-element-prompt-submit", { conversationId, ...(payload ?? {}) });
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
 */
function makeDesignModeInputHandler(gestureState: GestureState): DesignModeInputListener {
  return (_event, input) => {
    const type = input && input.type;
    // mouseDown = click-to-select; keyDown = Enter-to-submit; rawKeyDown = pre-IME.
    if (type === "mouseDown" || type === "keyDown" || type === "rawKeyDown") {
      gestureState.lastGestureAt = Date.now();
    }
  };
}

type GateError =
  | "browser IPC is only available to the connected server's page"
  | "no browser registry for this window";

type AgentViewError =
  | GateError
  | "No browser view"
  | "The previous browser page is no longer available"
  | "Current session host is required to read this browser view"
  | "Session host changed; the previous browser page is no longer available";

/**
 * Register every `omnigent:browser-*` IPC handler. Idempotent per process is
 * NOT guaranteed — call exactly once from main.js's registerIpc.
 */
function registerBrowserIpc<Event extends BrowserIpcEvent>({
  ipcMain,
  isPinnedOriginSender,
  getRegistryForEvent,
  getAgentContextForEvent = () => null,
  recoverAgentNavigationForEvent = async () => false,
  getAgentNavigationHintForEvent = () => null,
  // Injected so native dialogs can be parented to the sender's window.
  getWindowForEvent = () => null,
  chromeCookieStore = chromeCookieStoreModule,
  platform = process.platform,
  dialog,
  confirmCookieImport = (request) => askToImportChromeCookies(request, dialog),
}: BrowserIpcDeps<Event>): void {
  /**
   * Resolve the sender's registry after the privileged-origin gate. Returns
   * `{ registry }` on success or `{ error }` (a structured result, never a
   * throw) so the relay/toolbar surfaces a clean error.
   */
  const gateRegistry = (
    event: Event,
  ): { error: GateError } | { error?: undefined; registry: BrowserIpcRegistry } => {
    if (!isPinnedOriginSender(event)) {
      return { error: "browser IPC is only available to the connected server's page" };
    }
    const registry = getRegistryForEvent(event);
    if (!registry) return { error: "no browser registry for this window" };
    return { registry };
  };

  /** A window-scoped sender for the event's own webContents. Used to push
   *  url/nav-state pings back to exactly the renderer that drives the view. */
  const senderFor =
    (event: Event): BrowserPushSender =>
    (channel, ...args) => {
      try {
        push(event.sender, channel, ...args);
      } catch {
        /* window torn down */
      }
    };

  async function withAgentView(
    event: Event,
    args: unknown,
  ): Promise<{ error: AgentViewError } | { error?: undefined; entry: BrowserIpcEntry }> {
    const gated = gateRegistry(event);
    if (gated.error) return gated;
    const entry = gated.registry.get(optionalProperty(args, "conversationId"));
    if (!entry) return { error: "No browser view" };
    const context = entry.agentContext;
    if (context?.revoked) return { error: "The previous browser page is no longer available" };
    if (context?.hostIsThisMachine === true) {
      const opts = optionalProperty(args, "opts");
      const sourceHostId =
        optionalProperty(opts, "sessionHostProtocol") !== SESSION_HOST_PROTOCOL
          ? undefined
          : readProperty(opts, "sourceHostId");
      if (typeof sourceHostId !== "string" || !sourceHostId) {
        return { error: "Current session host is required to read this browser view" };
      }
      const currentContext = getAgentContextForEvent(event, opts);
      if (sourceHostId !== context.sourceHostId || currentContext?.hostIsThisMachine !== true) {
        await gated.registry.revokeAgentView(readProperty(args, "conversationId"));
        return { error: "Session host changed; the previous browser page is no longer available" };
      }
    }
    return { entry };
  }

  // Open (create-if-absent) or navigate a conversation's view, and measure it
  // into place. `force` reloads even on the same URL (agent "bring me back"
  // intent). Returns the registry's structured `{ ok, created, error }`.
  handle(ipcMain, "omnigent:browser-open-or-navigate", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const conversationId = optionalProperty(args, "conversationId");
    const url = optionalProperty(args, "url");
    const bounds = optionalProperty(args, "bounds");
    const opts = optionalProperty(args, "opts");
    if (typeof conversationId !== "string" || !conversationId) {
      return { ok: false, error: "conversationId is required" };
    }
    const isCurrentNavigation = g.registry.beginAgentNavigation(conversationId);
    const cancelled = { ok: false, created: false, error: "navigation superseded or closed" };
    const isCurrent = () => isCurrentNavigation() && isPinnedOriginSender(event);
    const navigate = async (registry: BrowserIpcRegistry): Promise<RegistryOpenResult> => {
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
        force: !!optionalProperty(opts, "force"),
        // Renderer flags cannot prove user intent; enforce policy for every
        // renderer navigation, including the URL bar.
        agent: true,
        agentContext,
      });
    };
    let r = await navigate(g.registry);
    if (!isCurrent()) return cancelled;
    let recovered = false;
    if (!r.ok && optionalProperty(opts, "agent")) {
      try {
        recovered = await recoverAgentNavigationForEvent(
          event,
          url,
          optionalProperty(opts, "sourceHostId"),
          optionalProperty(opts, "rememberedArcaHostId"),
          optionalProperty(opts, "sourceIsArcaHost"),
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
      !r.ok && optionalProperty(opts, "agent")
        ? getAgentNavigationHintForEvent(
            event,
            url,
            optionalProperty(opts, "sourceHostId"),
            optionalProperty(opts, "rememberedArcaHostId"),
            optionalProperty(opts, "sourceIsArcaHost"),
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
  handle(ipcMain, "omnigent:browser-set-active", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const conversationId = optionalProperty(args, "conversationId") ?? null;
    const r = g.registry.setActive(conversationId);
    // A tab the user returns to gets keyboard focus back in its page.
    if (r.ok && conversationId !== null && optionalProperty(args, "focus") === true) {
      g.registry.get(conversationId)?.view.webContents.focus();
    }
    return { ok: r.ok, error: r.error };
  });

  // The attached page if it has keyboard focus, so the renderer can tell a click
  // into that page apart from the window losing focus to another app.
  handle(ipcMain, "omnigent:browser-focused-view", (event) => {
    const g = gateRegistry(event);
    if (g.error) return null;
    const viewId = g.registry.activeConversationId();
    if (viewId === null) return null;
    try {
      return g.registry.get(viewId)?.view.webContents.isFocused() ? viewId : null;
    } catch {
      return null;
    }
  });

  // Hide/show the active view in place while a DOM overlay is open. The native
  // view always paints above the renderer, so z-index can't put a dialog/menu/
  // tooltip over it — the renderer ref-counts open overlays and toggles this.
  handle(ipcMain, "omnigent:browser-set-suppressed", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const r = g.registry.setSuppressed(!!optionalProperty(args, "suppressed"));
    return { ok: r.ok, error: r.error };
  });

  // Intercept embedded-page Ctrl+Tab only while a compatible renderer is
  // subscribed; older server UIs must retain ownership of the shortcut.
  handle(ipcMain, "omnigent:browser-set-recent-session-switch-supported", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    return g.registry.setRecentSessionSwitchSupported(!!optionalProperty(args, "supported"));
  });

  // Turn Cmd/Ctrl-clicked page links into new tabs only while a compatible
  // renderer handles `browser-open-tab`; older server UIs keep in-place loads.
  handle(ipcMain, "omnigent:browser-set-open-tab-supported", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    return g.registry.setOpenTabSupported(!!optionalProperty(args, "supported"));
  });

  // A supported renderer can still decline the gesture when no sessions exist.
  handle(ipcMain, "omnigent:browser-cancel-recent-session-switch", (event) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    return g.registry.cancelRecentSessionSwitch();
  });

  // Reposition the active conversation's view to freshly-measured bounds.
  handle(ipcMain, "omnigent:browser-resize", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const conversationId = optionalProperty(args, "conversationId");
    const bounds = optionalProperty(args, "bounds");
    if (typeof conversationId !== "string" || !conversationId) {
      return { ok: false, error: "conversationId is required" };
    }
    const entry = g.registry.get(conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    if (bounds) entry.boundsController.setRendererBounds(bounds);
    return { ok: true };
  });

  // Capture the conversation's view as a base64 PNG.
  handle(ipcMain, "omnigent:browser-screenshot", async (event, args) => {
    const g = await withAgentView(event, args);
    if (g.error) return { ok: false, error: g.error };
    const { entry } = g;
    try {
      const image = await entry.view.webContents.capturePage();
      const dataUrl = `data:image/png;base64,${image.toPNG().toString("base64")}`;
      return { ok: true, dataUrl };
    } catch (e) {
      return { ok: false, error: errorText(e) };
    }
  });

  // Both transports converge on main-owned scripts and the same sender gate.
  async function performBrowserAction(event: Event, args: unknown, legacy = false) {
    const g = await withAgentView(event, args);
    if (g.error) return { ok: false, error: g.error };
    const { entry } = g;
    try {
      const { action, params } = legacy
        ? decodeLegacyBrowserAction(optionalProperty(args, "js"))
        : {
            action: optionalProperty(args, "action"),
            params: optionalProperty(args, "params"),
          };
      const js = buildBrowserActionScript(action, params);
      const result = await entry.view.webContents.executeJavaScript(js, false);
      return { ok: true, result: typeof result === "string" ? result : JSON.stringify(result) };
    } catch (e) {
      return { ok: false, error: errorText(e) };
    }
  }
  handle(ipcMain, "omnigent:browser-perform-action", (event, args) =>
    performBrowserAction(event, args),
  );
  handle(ipcMain, "omnigent:browser-execute", (event, args) =>
    performBrowserAction(event, args, true),
  );

  // Whether a view currently exists for a conversation. Lets a (re)mounting
  // pane re-attach an already-created view without waiting for a create event.
  handle(ipcMain, "omnigent:browser-has-view", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { exists: false };
    const conversationId = optionalProperty(args, "conversationId");
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
  handle(ipcMain, "omnigent:browser-close", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const conversationId = optionalProperty(args, "conversationId");
    const reason = optionalProperty(args, "reason");
    const r = g.registry.close(conversationId, reason);
    return { ok: r.ok, removed: r.removed ?? false };
  });

  // ── Toolbar: history navigation ──────────────────────────────────────────
  // Back / forward / reload. Each returns fresh nav-state so the caller updates
  // button-disabled immediately without waiting for the did-navigate event.

  handle(ipcMain, "omnigent:browser-go-back", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const entry = g.registry.get(optionalProperty(args, "conversationId"));
    if (!entry) return { ok: false, error: "No browser view" };
    goBack(entry.view.webContents);
    return { ok: true, ...readNavState(entry.view.webContents) };
  });

  handle(ipcMain, "omnigent:browser-go-forward", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const entry = g.registry.get(optionalProperty(args, "conversationId"));
    if (!entry) return { ok: false, error: "No browser view" };
    goForward(entry.view.webContents);
    return { ok: true, ...readNavState(entry.view.webContents) };
  });

  handle(ipcMain, "omnigent:browser-reload", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const entry = g.registry.get(optionalProperty(args, "conversationId"));
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
  handle(ipcMain, "omnigent:open-browser-devtools", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const entry = g.registry.get(optionalProperty(args, "conversationId"));
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
      return { ok: false, error: errorText(e) };
    }
  });

  // ── Design mode (point-and-prompt) ───────────────────────────────────────
  // Enable/disable the in-page picker and signal a submit result back to the
  // popup. Listeners are stored per-entry (and detached by the registry's
  // close()) so a late background-conversation marker can't leak into another UI.

  handle(ipcMain, "omnigent:browser-enable-design-mode", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const conversationId = optionalProperty(args, "conversationId");
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
      const msg = errorText(e);
      if (msg.includes("Object has been destroyed")) return { ok: false, error: "browser closed" };
      return { ok: false, error: msg };
    }
  });

  handle(ipcMain, "omnigent:browser-disable-design-mode", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const conversationId = optionalProperty(args, "conversationId");
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

  const siteForEntry = (entry: BrowserIpcEntry): { error: string } | { host: string } => {
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

  handle(ipcMain, "omnigent:browser-chrome-cookie-preview", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    if (platform !== "darwin") {
      return { ok: false, error: CHROME_COOKIE_IMPORT_PLATFORM_ERROR };
    }
    const entry = g.registry.get(optionalProperty(args, "conversationId"));
    if (!entry) return { ok: false, error: "No browser view" };
    const site = siteForEntry(entry);
    if ("error" in site) return { ok: false, error: site.error };
    try {
      return await chromeCookieStore.previewSiteCookies({ host: site.host });
    } catch (e) {
      return { ok: false, error: errorText(e) };
    }
  });

  handle(ipcMain, "omnigent:browser-chrome-cookie-import", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    if (platform !== "darwin") {
      return { ok: false, error: CHROME_COOKIE_IMPORT_PLATFORM_ERROR };
    }
    const conversationId = optionalProperty(args, "conversationId");
    const profileId = optionalProperty(args, "profileId");
    const expectedHost = optionalProperty(args, "expectedHost");
    const scope = optionalProperty(args, "scope");
    const entry = g.registry.get(conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    const site = siteForEntry(entry);
    if ("error" in site) return { ok: false, error: site.error };
    // Fall back to the real module only for stores that are pure stubs.
    const normalizeHost = chromeCookieStore.normalizeHost ?? chromeCookieStoreModule.normalizeHost;
    const safeNormalize = (value: unknown) => {
      try {
        return normalizeHost(value);
      } catch {
        return null;
      }
    };
    const hostMatches = (value: unknown) => {
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
          return !("error" in current) && hostMatches(current.host);
        },
      });
    } catch (e) {
      const msg = errorText(e);
      if (msg.includes("Object has been destroyed")) return { ok: false, error: "browser closed" };
      return { ok: false, error: msg };
    }
  });

  // Forward a submit's result envelope into the page for green/red feedback.
  // `id` matches the page's submitId so a late callback can't paint over a fresh
  // popup. Fields are defensively coerced before crossing back into the page.
  handle(ipcMain, "omnigent:browser-signal-design-result", async (event, payload) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    if (!payload || typeof payload !== "object") return { ok: false, error: "bad payload" };
    const entry = g.registry.get(readProperty(payload, "conversationId"));
    if (!entry) return { ok: false, error: "No browser view" };
    const id = readProperty(payload, "id");
    const ok = !!readProperty(payload, "ok");
    const message = readProperty(payload, "message");
    const safe = {
      id: typeof id === "number" ? id : 0,
      ok,
      message: typeof message === "string" ? message : "",
    };
    try {
      await entry.view.webContents.executeJavaScript(
        `window.__omniOnDesignResult && window.__omniOnDesignResult(${JSON.stringify(safe)})`,
      );
      return { ok: true };
    } catch (e) {
      const msg = errorText(e);
      if (msg.includes("Object has been destroyed")) return { ok: false, error: "browser closed" };
      return { ok: false, error: msg };
    }
  });
}

export {
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
