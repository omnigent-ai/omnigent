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

import type { Rectangle } from "electron";

import {
  checkAgentNavigation,
  isAgentConnectionAllowed,
  isAgentNavigationAllowed,
  isDirectOnly,
  isLocalhostSubdomain,
  isProxyAddress,
  lookupHostAddresses,
  type NavigationVerdict,
} from "./browserUrlPolicy.ts";
import type {
  BrowserViewBoundsController,
  BrowserViewBoundsControllerOptions,
  RendererBounds,
} from "./browserViewBounds.ts";

export const DEFAULT_CAP = 10;

/** Who asked for an agent navigation; the host decides whether it may reach loopback. */
export interface AgentContext {
  hostIsThisMachine?: boolean;
  sourceHostId?: string | null;
  /** Set once the view's page from this context is being cleared. */
  revoked?: boolean;
}

/** The parts of a `context-menu` event's params the pane menu reads. */
export interface ContextMenuParams {
  linkURL?: string;
  selectionText?: unknown;
}

/** Plain menu data; the host builds the Electron Menu. */
export type ContextMenuItem = { label: string; click: () => void } | { type: "separator" };

/** The parts of a `before-input-event` input the recent-session forwarding reads. */
export interface BrowserViewInput {
  type?: string;
  key?: string;
  code?: string;
  control?: boolean;
  shift?: boolean;
  alt?: boolean;
  meta?: boolean;
  isAutoRepeat?: boolean;
}

interface PreventableEvent {
  preventDefault: () => void;
}

type NavigationGuard = (event: PreventableEvent, targetUrl: string) => void;

/** Any webContents listener that design mode stores on an entry. */
export type DesignModeListener = (...args: never[]) => void;

/** The webContents design mode attached its listeners to. */
export interface DesignModeWebContents {
  removeListener: (
    event: "console-message" | "input-event",
    listener: DesignModeListener,
  ) => unknown;
}

/** The parts of a view's webContents the registry drives. */
export interface BrowserViewWebContents {
  id: number;
  loadURL: (url: string) => unknown;
  close: () => void;
  copy: () => void;
  on: ((
    event: "context-menu",
    listener: (event: unknown, params: ContextMenuParams) => void,
  ) => unknown) &
    ((event: "will-navigate" | "will-redirect", listener: NavigationGuard) => unknown) &
    ((
      event: "will-frame-navigate",
      listener: (event: PreventableEvent & { url: string }) => void,
    ) => unknown) &
    ((
      event: "before-input-event",
      listener: (event: PreventableEvent, input: BrowserViewInput) => void,
    ) => unknown) &
    ((event: "blur", listener: () => void) => unknown);
  setWindowOpenHandler: (
    handler: (details: { url: string; disposition: string }) => { action: "deny" },
  ) => void;
}

/** The parts of a WebContentsView the registry drives. */
export interface BrowserView {
  setBounds: (bounds: Rectangle) => void;
  setVisible: (visible: boolean) => void;
  webContents: BrowserViewWebContents;
}

export interface BrowserViewOptions {
  webPreferences: {
    partition: string;
    nodeIntegration: false;
    contextIsolation: true;
    sandbox: true;
  };
}

/** One view and its per-view state. */
export interface BrowserViewEntry<View extends BrowserView> {
  conversationId: string;
  view: View;
  boundsController: BrowserViewBoundsController;
  lastRequestedUrl: string;
  navigationPolicyEnforced: boolean;
  agentContext: AgentContext | null;
  designModeListener: DesignModeListener | null;
  designModeInputListener: DesignModeListener | null;
  designModeWebContents: DesignModeWebContents | null;
  recentSessionSwitching: boolean;
}

export interface BrowserViewRegistryDeps<View extends BrowserView> {
  /** (opts) => new WebContentsView(opts) — injectable for tests */
  WebContentsViewCtor: (options: BrowserViewOptions) => View;
  /** bounds-controller factory (createBrowserViewBoundsController) */
  createBoundsController: (
    options: BrowserViewBoundsControllerOptions,
  ) => BrowserViewBoundsController;
  /** (view) => mainWindow.contentView.addChildView(view) */
  attachToHost: (view: View) => void;
  /** (view) => mainWindow.contentView.removeChildView(view) */
  detachFromHost: (view: View) => void;
  /** (channel, payload) => mainWindow.webContents.send(...) */
  sendToRenderer: (channel: string, payload: Record<string, unknown>) => void;
  getHostZoomFactor?: () => number;
  getHostDisplayScaleFactor?: () => number | null;
  isHostFocused?: () => boolean;
  openUrlExternal?: (url: string) => void;
  copyTextToClipboard?: (text: string) => void;
  showContextMenu?: (items: ContextMenuItem[]) => void;
  /** Dismiss shell-owned UI when an overlay hides the pane. */
  onSuppressionChange?: (suppressed: boolean) => void;
  allowsLocalhost?: (context: AgentContext | null | undefined) => boolean;
  /** Addresses for the agent DNS check, resolved in the view's partition. */
  resolveHost?: (hostname: string, partition: string) => Promise<string[]>;
  /** "DIRECT" or a PAC-style proxy list for `url` in the view's partition. */
  resolveProxy?: (url: string, partition: string) => Promise<string>;
  cap?: number;
  partitionScope?: string;
}

export interface OpenOrNavigateOptions {
  force?: boolean;
  /** Apply the agent navigation policy to this URL and its redirects. */
  agent?: boolean;
  agentContext?: AgentContext | null;
}

export interface OkResult {
  ok: true;
}
export interface ErrorResult {
  ok: false;
  error: string;
}

export type GetOrCreateResult<View extends BrowserView> =
  | { ok: true; entry: BrowserViewEntry<View>; created: boolean }
  | { ok: false; error: string; cap: number };

export type OpenOrNavigateResult<View extends BrowserView> = GetOrCreateResult<View> | ErrorResult;

/** The request fields Electron's `webRequest` hooks pass. */
export interface AgentRequestDetails {
  webContentsId?: number;
  url: string;
}

export interface AgentResponseDetails extends AgentRequestDetails {
  ip?: string;
  resourceType?: string;
  statusCode?: number;
}

type RequestHeaders = Record<string, string | string[]>;

/** The navigation policy for a request from an agent view or its partition. */
interface RequestPolicy {
  allowLocalhost: boolean;
  conversationId?: string;
  partition: string;
}

export interface BrowserViewRegistry<View extends BrowserView> {
  get: (conversationId: string) => BrowserViewEntry<View> | null;
  getOrCreate: (conversationId: string) => GetOrCreateResult<View>;
  openOrNavigate: (
    conversationId: string,
    url: string | null | undefined,
    bounds?: RendererBounds | null,
    opts?: OpenOrNavigateOptions,
  ) => OpenOrNavigateResult<View>;
  /** Start an agent navigation; the returned check is false once a newer one starts. */
  beginAgentNavigation: (conversationId: string) => () => boolean;
  needsAgentViewRevocation: (
    entry: BrowserViewEntry<View> | null | undefined,
    nextContext: AgentContext | null | undefined,
  ) => boolean;
  revokeAgentView: (
    conversationId: string,
    isCurrent?: () => boolean,
  ) => Promise<OkResult | ErrorResult>;
  checkAgentNavigation: (
    conversationId: string,
    url: unknown,
    agentContext: AgentContext | null | undefined,
  ) => Promise<NavigationVerdict>;
  shouldCancelAgentRequest: (details: AgentRequestDetails, partition?: string) => Promise<boolean>;
  agentRequestHeaders: (
    details: { webContentsId?: number; requestHeaders?: RequestHeaders },
    partition?: string,
  ) => { requestHeaders?: RequestHeaders };
  shouldCancelAgentResponse: (
    details: AgentResponseDetails,
    partition?: string,
  ) => Promise<boolean>;
  setActive: (conversationId: string | null | undefined) => OkResult | ErrorResult;
  setSuppressed: (suppressed: unknown) => OkResult;
  cancelRecentSessionSwitch: () => OkResult;
  setRecentSessionSwitchSupported: (supported: unknown) => OkResult;
  setOpenTabSupported: (supported: unknown) => OkResult;
  close: (conversationId: string, reason?: string | null) => { ok: true; removed: boolean };
  closeAll: (reason?: string | null) => void;
  activeConversationId: () => string | null;
  isSuppressed: () => boolean;
  size: () => number;
  has: (conversationId: string) => boolean;
  forEach: (fn: (entry: BrowserViewEntry<View>, conversationId: string) => void) => void;
  cap: number;
}

// Map WebSocket handshakes onto the HTTP URL rules.
function httpUrlForWebSocket(url: string): string {
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
 * @param scope Registry-unique namespace (one per shell window).
 * @param viewId Conversation ID or encoded browser-tab view ID.
 */
export function agentPartition(scope: string, viewId: string): string {
  let conversationId = viewId;
  // Session IDs are UUID hex strings; tab keys follow browserViewId in
  // web/src/hooks/useBrowserTabs.ts. Only storage uses the owning session ID.
  const tab = /^browser-tab:([^:]+):[^:]+$/.exec(viewId);
  if (tab?.[1] !== undefined) {
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

export function createBrowserViewRegistry<View extends BrowserView>({
  WebContentsViewCtor,
  createBoundsController,
  attachToHost,
  detachFromHost,
  sendToRenderer,
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
}: BrowserViewRegistryDeps<View>): BrowserViewRegistry<View> {
  type Entry = BrowserViewEntry<View>;
  const entries = new Map<string, Entry>();
  const pendingNavigations = new Map<string, symbol>();

  function beginAgentNavigation(conversationId: string): () => boolean {
    const generation = Symbol();
    pendingNavigations.set(conversationId, generation);
    return () => pendingNavigations.get(conversationId) === generation;
  }
  const allowsLocalhost = (context: AgentContext | null | undefined) =>
    !context?.revoked && contextAllowsLocalhost(context);

  function needsAgentViewRevocation(
    entry: Entry | null | undefined,
    nextContext: AgentContext | null | undefined,
  ): boolean {
    const context = entry?.agentContext;
    return (
      context?.hostIsThisMachine === true &&
      (context.revoked ||
        !nextContext?.hostIsThisMachine ||
        nextContext.sourceHostId !== context.sourceHostId)
    );
  }

  async function revokeAgentView(
    conversationId: string,
    isCurrent = beginAgentNavigation(conversationId),
  ): Promise<OkResult | ErrorResult> {
    const entry = get(conversationId);
    const context = entry?.agentContext;
    if (!entry || !context) return { ok: true };
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
  let activeConversationId: string | null = null;
  // When true, the active view is hidden in place (setVisible(false)) so DOM
  // overlays (dialogs, menus, tooltips, toasts) aren't covered by the native
  // layer, which always paints above the renderer regardless of z-index. Sticky
  // across attaches: a view that becomes active while suppressed stays hidden.
  let overlaySuppressed = false;
  let recentSessionSwitchSupported = false;
  // Set by an SPA that opens `browser-open-tab` requests as new tabs; an older
  // SPA would drop them, so modifier-clicks navigate in place until declared.
  let openTabSupported = false;

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

  function setSuppressed(suppressed: unknown): OkResult {
    overlaySuppressed = !!suppressed;
    applyActiveVisibility();
    onSuppressionChange(overlaySuppressed);
    return { ok: true };
  }

  function makeEntry(conversationId: string, view: View): Entry {
    const entry: Entry = {
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

  function get(conversationId: string): Entry | null {
    return entries.get(conversationId) || null;
  }

  function getOrCreate(conversationId: string): GetOrCreateResult<View> {
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

  // Popups never open the system browser (an abuse vector for agent-visited
  // pages): background-tab clicks go to a supporting renderer as new tabs, others
  // load in this view. loadURL skips will-navigate, so enforce policy here.
  function installWindowOpenPolicy(entry: Entry): void {
    const wc = entry.view && entry.view.webContents;
    if (!wc || typeof wc.setWindowOpenHandler !== "function") return;
    wc.setWindowOpenHandler(({ url, disposition }) => {
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
      if (disposition === "background-tab" && openTabSupported) {
        sendToRenderer("browser-open-tab", { conversationId: entry.conversationId, url });
        return { action: "deny" };
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
  function attachViewContextMenu(entry: Entry): void {
    const wc = entry.view && entry.view.webContents;
    if (!wc || typeof wc.on !== "function") return;
    wc.on("context-menu", (_event, params) => {
      const items: ContextMenuItem[] = [];
      const { linkURL } = params;
      if (linkURL) {
        if (/^https?:\/\//i.test(linkURL)) {
          items.push({
            label: "Open Link in Browser",
            click: () => openUrlExternal(linkURL),
          });
        }
        items.push({
          label: "Copy Link Address",
          click: () => copyTextToClipboard(linkURL),
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
  function attachAgentNavGuard(conversationId: string, entry: Entry): void {
    const wc = entry.view && entry.view.webContents;
    if (!wc || typeof wc.on !== "function") return;
    const guard: NavigationGuard = (event, targetUrl) => {
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
  function cancelRecentSessionInput(entry: Entry, notifyRenderer: boolean): void {
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

  function cancelRecentSessionSwitch(): OkResult {
    if (activeConversationId === null) return { ok: true };
    const entry = entries.get(activeConversationId);
    if (entry) cancelRecentSessionInput(entry, false);
    return { ok: true };
  }

  function setRecentSessionSwitchSupported(supported: unknown): OkResult {
    recentSessionSwitchSupported = !!supported;
    if (!recentSessionSwitchSupported) {
      entries.forEach((entry) => cancelRecentSessionInput(entry, false));
    }
    return { ok: true };
  }

  function setOpenTabSupported(supported: unknown): OkResult {
    openTabSupported = !!supported;
    return { ok: true };
  }

  function attachRecentSessionInput(entry: Entry): void {
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

  function checkAgentNavigationWithPolicy(
    url: unknown,
    { allowLocalhost, partition }: { allowLocalhost: boolean; partition: string },
  ): Promise<NavigationVerdict> {
    return checkAgentNavigation(url, {
      allowLocalhost,
      resolveHost: (hostname) => resolveHost(hostname, partition),
    });
  }

  // The IPC first hop uses the same partition resolver as network preflight.
  function checkAgentNavigationFor(
    conversationId: string,
    url: unknown,
    agentContext: AgentContext | null | undefined,
  ): Promise<NavigationVerdict> {
    return checkAgentNavigationWithPolicy(url, {
      allowLocalhost: allowsLocalhost(agentContext),
      partition: agentPartition(partitionScope, conversationId),
    });
  }

  function entryForWebContents(webContentsId: number | undefined): Entry | null {
    for (const entry of entries.values()) {
      try {
        if (entry.view.webContents.id === webContentsId) return entry;
      } catch {
        continue; // destroyed
      }
    }
    return null;
  }

  function requestPolicyFor(
    webContentsId: number | undefined,
    partition: string | undefined,
  ): RequestPolicy | null {
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

  function hostnameOf(url: string): string {
    return URL.canParse(url) ? new URL(url).hostname : "";
  }

  // Resolve named hosts in the owning partition before any request is sent.
  // IP literals and exact loopback names need only the URL check; `*.localhost`
  // must also be on a direct route so a proxy never receives the request.
  async function shouldCancelAgentRequest(
    { webContentsId, url }: AgentRequestDetails,
    partition?: string,
  ): Promise<boolean> {
    const policy = requestPolicyFor(webContentsId, partition);
    if (!policy) return false;
    const policyUrl = httpUrlForWebSocket(url);
    let verdict = await checkAgentNavigationWithPolicy(policyUrl, policy);
    const hostname = hostnameOf(policyUrl);
    if (verdict.ok && isLocalhostSubdomain(hostname)) {
      verdict = await checkDirectLocalhostRoute(url, hostname, policy.partition);
    }
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
  function agentRequestHeaders(
    { webContentsId, requestHeaders }: { webContentsId?: number; requestHeaders?: RequestHeaders },
    partition?: string,
  ): { requestHeaders?: RequestHeaders } {
    if (!requestPolicyFor(webContentsId, partition)) return {};
    const headers: RequestHeaders = {};
    const directives = new Map<string, string>(); // lowercased → as sent, keeping e.g. no-store
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

  // Chromium pins `*.localhost` to loopback, so only a proxy could send it
  // anywhere else; an unknown route counts as proxied. WebSockets keep their
  // ws(s) URL here because Chromium picks their proxy by that scheme.
  async function checkDirectLocalhostRoute(
    requestUrl: string,
    hostname: string,
    partition: string,
  ): Promise<NavigationVerdict> {
    const blocked: NavigationVerdict = {
      ok: false,
      error: `navigation blocked: host "${hostname}" may be routed through a proxy`,
    };
    try {
      return isDirectOnly(await resolveProxy(requestUrl, partition)) ? { ok: true } : blocked;
    } catch {
      return blocked;
    }
  }

  async function shouldCancelAgentResponse(
    { webContentsId, url, ip, resourceType, statusCode }: AgentResponseDetails,
    partition?: string,
  ): Promise<boolean> {
    const policy = requestPolicyFor(webContentsId, partition);
    if (!policy) return false;
    const policyUrl = httpUrlForWebSocket(url);
    const hostname = hostnameOf(policyUrl);
    const localhostSubdomain = isLocalhostSubdomain(hostname);
    // WebSocket upgrades omit the connected IP.
    const ipLessUpgrade = !ip && resourceType === "webSocket" && statusCode === 101;
    let verdict: NavigationVerdict;
    if (policy.allowLocalhost && localhostSubdomain) {
      // A loopback IP may be a local proxy's, so `*.localhost` also needs a direct route.
      verdict = ipLessUpgrade ? { ok: true } : isAgentConnectionAllowed(policyUrl, ip, policy);
      if (verdict.ok) {
        verdict = await checkDirectLocalhostRoute(url, hostname, policy.partition);
      }
    } else if (ipLessUpgrade) {
      // Repeating the DNS check cannot rule out a DNS change before the connection.
      verdict = await checkAgentNavigationWithPolicy(policyUrl, policy);
    } else {
      verdict = isAgentConnectionAllowed(policyUrl, ip, policy);
    }
    if (verdict.ok) return false;
    // Through a proxy `ip` is the proxy's address; accept it only if it is one
    // (same-IP limit documented on isProxyAddress). A proxy would resolve
    // `*.localhost` on its own network, so those must connect to loopback.
    const proxied =
      !!ip &&
      !localhostSubdomain &&
      (await isProxyAddress(ip, await resolveProxy(policyUrl, policy.partition), (proxyHost) =>
        resolveHost(proxyHost, policy.partition),
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

  function openOrNavigate(
    conversationId: string,
    url: string | null | undefined,
    bounds?: RendererBounds | null,
    opts?: OpenOrNavigateOptions,
  ): OpenOrNavigateResult<View> {
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
      entry.agentContext = entry.navigationPolicyEnforced ? (opts?.agentContext ?? null) : null;
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
          const message = (e as { message?: unknown } | null | undefined)?.message;
          return { ok: false, error: `loadURL failed: ${e && message ? message : e}` };
        }
      }
    }
    return { ok: true, entry, created };
  }

  function setActive(conversationId: string | null | undefined): OkResult | ErrorResult {
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

  function close(conversationId: string, reason?: string | null): { ok: true; removed: boolean } {
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

  function closeAll(reason?: string | null): void {
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
    setOpenTabSupported,
    close,
    closeAll,
    // Introspection
    activeConversationId: () => activeConversationId,
    isSuppressed: () => overlaySuppressed,
    size: () => entries.size,
    has: (conversationId: string) => entries.has(conversationId),
    forEach: (fn) => entries.forEach(fn),
    // Constants exposed for tests / main.js wiring
    cap,
  };
}
