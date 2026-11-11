// Types of the objects the preloads expose to their pages with contextBridge (types only).
// The web imports them with `import type` and makes the main-window bridge's members optional
// for older shells; here a member is optional only when the current preload adds it conditionally.

/** Unsubscribes a bridge listener. */
export type Unsubscribe = () => void;

/** Theme source the user selected: an explicit scheme or the OS setting. */
export type ThemeSource = "light" | "dark" | "system";

export interface NativeNotifyParams {
  /** Headline — typically the conversation's display label. */
  title: string;
  /** Secondary line, e.g. "Agent finished and is ready for your input." */
  body?: string;
  /**
   * In-app path the shell should open when the user clicks this notification,
   * e.g. `"/c/conv_abc123"`. A click closure can't cross the process boundary,
   * so we forward the destination as a string and route to it on click via
   * `onNativeNotificationActivated`. Omitted -> click only focuses the window.
   */
  navigatePath?: string;
}

/** A native menu command for this window, as the preload forwards it. */
export interface DesktopCommandMessage {
  /** One of the shell's menu commands; a newer shell may send unknown ones. */
  command: string;
  /** The embedded browser view that held keyboard focus when Close fired, else null. */
  browserViewId: string | null;
  /** Whether closing this window quits the app (its last window, off macOS). */
  closingQuits: boolean;
  /** For Quit: what quitting stops (local sessions, a local server), else null. */
  quitDetail: string | null;
}

/** Menu commands this page can act on now, plus the open session's state. */
export interface DesktopCommandStateReport {
  commands: string[];
  sessionPinned: boolean;
  sessionArchived: boolean;
}

export interface BrowserHostOptions {
  sourceHostId?: string | null;
  sessionHostProtocol?: number;
}

/** Options for opening or navigating a conversation's embedded browser view. */
export interface BrowserOpenOptions extends BrowserHostOptions {
  /** Reload even when the view is already on the same URL. */
  force?: boolean;
  /** Model-issued navigation, which main gates behind its allowlist. */
  agent?: boolean;
  rememberedArcaHostId?: string;
  sourceIsArcaHost?: boolean;
}

/** Placeholder bounds of a browser view, in CSS pixels of the host page. */
export interface BrowserViewBounds {
  x: number;
  y: number;
  width: number;
  height: number;
  devicePixelRatio?: number;
}

export interface BrowserCallResult {
  ok: boolean;
  error?: string;
}

export interface BrowserOpenResult extends BrowserCallResult {
  created?: boolean;
  hostIsThisMachine?: boolean;
}

export interface BrowserNavResult extends BrowserCallResult {
  canGoBack?: boolean;
  canGoForward?: boolean;
}

export interface BrowserScreenshotResult extends BrowserCallResult {
  /** Base64 PNG data URL. */
  dataUrl?: string;
}

/** Result of a fixed browser action; `result` is the page's reply as a string. */
export interface BrowserScriptResult extends BrowserCallResult {
  result?: string;
}

export interface BrowserCloseResult extends BrowserCallResult {
  removed?: boolean;
}

/** Whether a conversation's view exists, and its current page when it does. */
export interface BrowserViewState {
  exists: boolean;
  hostIsThisMachine?: boolean;
  url?: string;
  /** The URL a new view is loading before it has committed one. */
  pendingUrl?: string;
  title?: string;
  canGoBack?: boolean;
  canGoForward?: boolean;
}

/** Fixed actions `browserPerformAction` accepts. */
export type BrowserAction = "snapshot" | "click" | "type";

export interface BrowserRecentSessionInput {
  type: "keydown" | "keyup";
  key: "Tab" | "Control" | "Escape";
  code: string;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  metaKey: boolean;
  repeat: boolean;
}

export interface BrowserUrlChange {
  conversationId: string;
  url: string;
  title: string;
}

export interface BrowserNavState {
  conversationId: string;
  canGoBack: boolean;
  canGoForward: boolean;
}

/** Design-mode element info from the page, plus a cropped screenshot of it. */
export interface BrowserElementSelection {
  conversationId: string;
  /** PNG data URL, or null when the element had no visible area. */
  screenshot: string | null;
  [field: string]: unknown;
}

/** A design-mode prompt submit; the page supplies `id`, `element` and `prompt`. */
export interface BrowserElementPromptSubmit {
  conversationId: string;
  [field: string]: unknown;
}

/** Result a design-mode submit paints into the page's popup. */
export interface BrowserDesignResult {
  /** The submit id the popup emitted. */
  id: number;
  ok: boolean;
  message?: string;
}

/** One importable site tree and how many cookies a profile holds for it. */
export interface ChromeCookieScopeCount {
  /** The page's host or one of its parent domains. */
  host: string;
  /** Cookies on that domain and its subdomains, plus inherited domain cookies. */
  cookieCount: number;
}

/** One local Chrome profile that holds cookies for the current site. */
export interface ChromeCookieProfile {
  /** Chrome's profile directory name (e.g. `Default`, `Profile 2`). */
  id: string;
  /** Chrome's profile display name, falling back to the directory name. */
  name: string;
  /** How many cookies the profile holds for the page's own host tree. */
  cookieCount: number;
  /** Counts per scope, narrowest first (see {@link ChromeCookiePreviewResult.scopes}).
   *  Only scopes this profile actually holds cookies for are listed. */
  scopes?: ChromeCookieScopeCount[];
}

export interface ChromeCookiePreviewResult {
  ok: boolean;
  /** Host whose page asked for the import (main-derived from the real URL). */
  host?: string;
  /** Scopes the user may pick: `host`, then each parent domain Chrome holds
   *  a domain cookie for. */
  scopes?: string[];
  /** Profiles with at least one cookie for any scope; empty when none match. */
  profiles?: ChromeCookieProfile[];
  error?: string;
}

export interface ChromeCookieImportArgs {
  profileId: string;
  /** Must still match the view's current site. */
  expectedHost: string;
  /** The site tree to copy: the page's host or a parent domain. */
  scope?: string;
}

export interface ChromeCookieImportResult {
  ok: boolean;
  /** The user declined the shell's own confirmation; nothing was read. */
  cancelled?: boolean;
  host?: string;
  /** The site tree actually copied (the page's host unless the user widened it). */
  scope?: string;
  profileId?: string;
  profileName?: string;
  /** Cookies matching the site tree (before decryption). */
  found?: number;
  /** Cookies written into the conversation's session. */
  imported?: number;
  /** Matched cookies we could not decrypt and left out. */
  skipped?: number;
  /** Cookies Chrome believes are valid but the session rejected. */
  failed?: number;
  error?: string;
}

/** A lifecycle action for the host daemon. */
export type HostControlAction = "start" | "stop" | "restart";

export type ArcaExtendMode = "overnight" | "workweek";
export type ArcaExtendResult =
  { ok: true; shutdownAt: number | null } | { ok: false; error: string };
export type DbcertRefreshResult = { ok: true } | { ok: false; error: string };
export interface DbcertRefreshOptions {
  skipArcaSync?: boolean;
}
export type DbcertStatusResult = { ok: true; valid: boolean } | { ok: false; error: string };

/** Result of a host control action from the desktop shell. */
export interface HostActionResult {
  ok: boolean;
  error?: string;
  /**
   * True when the failure was an authentication/sign-in problem — e.g. the
   * server needs a Databricks/OIDC login the desktop couldn't complete
   * headlessly — so the UI can offer a sign-in/retry affordance rather than a
   * generic error. Set by the desktop's `omnigent:host-control` handler.
   */
  authError?: boolean;
}

/** Result of connecting the Arca instance as a host, from the desktop shell. */
export interface ArcaConnectResult extends HostActionResult {
  /** Actual daemon identity captured on Arca for this selected server target. */
  identity?: { serverUrl: string; hostId: string };
  /**
   * The box's host daemon was already connected to this server (the command
   * reused it) — so no new host will appear in the host list.
   */
  alreadyRunning?: boolean;
  /**
   * The user deliberately declined or dismissed the connect console (before
   * or during the run) — not a failure; UIs should stay silent.
   */
  canceled?: boolean;
  /**
   * The outcome (success or failure) was already displayed in the connect
   * console's terminal — UIs must not echo it a second time.
   */
  shownInConsole?: boolean;
}

/**
 * Desktop-shell feature gates the server can't know about. All fields
 * optional: an older shell reports fewer.
 */
export interface DesktopFeatures {
  /**
   * Managed features (e.g. the Arca host option) are enabled.
   * Already scoped by the shell: true only when the flag is set AND the
   * window's server is a managed server.
   */
  databricksInternalFeatures?: boolean;
  /** This window may offer one-click Arca extension. */
  canExtendArca?: boolean;
  /** This window may renew the laptop's dbcert. */
  canRefreshDbcert?: boolean;
  /** Shell can restore Arca preview identity after a desktop restart. */
  arcaPreviewRecovery?: boolean;
  /** This shell can import the current site's cookies from local Google Chrome. */
  chromeCookieImport?: boolean;
}

/** Status of the local `omni` CLI, from the desktop shell. */
export interface CliStatus {
  /** Whether the CLI was found and is runnable. */
  installed: boolean;
  /** The resolved binary path (configured override or auto-detected), or null. */
  path: string | null;
  /** The CLI's reported version, or null. */
  version: string | null;
  /** How the path was resolved: an explicit override, PATH, or a known location. */
  source: "configured" | "path" | "candidate" | null;
  /** The install one-liner to show when the CLI is missing. */
  installCommand: string;
  /** Whether a just-submitted path was accepted (present on pick/set results). */
  accepted?: boolean;
  /** Policy disables set/browse/reset of custom CLI paths. */
  customizationDisabled?: boolean;
}

/** This machine's identity, read from local config (fast — no subprocess). */
export interface HostIdentity {
  /** Whether the `omnigent` CLI was found and is runnable. */
  cliInstalled: boolean;
  /** This machine's host id, or null if it has none yet. */
  hostId: string | null;
}

/** Where desktop onboarding connected a runner: this machine, or a remote host like Arca. */
export type OnboardingRunner = "local" | "remote";

/** The "Show Omnigent from anywhere" shortcut; `accelerator` is an Electron accelerator. */
export interface GlobalShortcutSetting {
  enabled: boolean;
  accelerator: string;
}

/** A chord to save, or `{ enabled: false }` to turn the shortcut off and keep its chord. */
export type GlobalShortcutChange = GlobalShortcutSetting | { enabled: false; accelerator?: never };

/** `setting` is what the shell saved, which may differ from what a stale window showed. */
export interface GlobalShortcutSaveResult {
  ok: boolean;
  setting?: GlobalShortcutSetting;
  error?: string;
}

/** The saved shortcut, with `error` while it is on but the shell couldn't register it. */
export interface GlobalShortcutStatus extends GlobalShortcutSetting {
  error?: string;
}

export type UpdateMode = "none" | "manual" | "start" | "default";

export interface UpdateConfig {
  mode: UpdateMode;
  autoInstall: boolean;
  skippedVersion: string | null;
}

export type UpdateStatus = {
  /** Installed Electron app version; absent on older desktop shells. */
  currentVersion?: string;
} & (
  | {
      state: "idle" | "checking" | "none";
      info?: undefined;
      progress?: undefined;
      lastError?: string;
    }
  | {
      state: "available" | "downloaded";
      info?: { version: string; releaseNotes?: string };
      progress?: undefined;
      lastError?: string;
    }
  | {
      state: "downloading";
      info?: { version: string; releaseNotes?: string };
      progress?: { percent: number };
      lastError?: string;
    }
  | {
      state: "error-security";
      info?: { version: string; releaseNotes?: string };
      progress?: undefined;
      lastError?: string;
    }
);

/**
 * The update status the server page sees. The preload collapses the states the
 * in-page update banner renders on to `idle`, because update notices are
 * shell-owned.
 */
export type BannerSafeUpdateStatus = Extract<UpdateStatus, { state: "idle" | "checking" | "none" }>;

/** `omnigentDesktop.updates`: update preferences and checks, never notices. */
export interface ElectronDesktopUpdatesBridge {
  getConfig: () => Promise<UpdateConfig>;
  getStatus: () => Promise<BannerSafeUpdateStatus>;
  check: () => Promise<void>;
  download: () => Promise<void>;
  installNow: () => Promise<void>;
  setConfig: (patch: Partial<UpdateConfig>) => Promise<UpdateConfig>;
  onStatus: (callback: (status: BannerSafeUpdateStatus) => void) => Unsubscribe;
  /** Current shell-owned update-overlay card height in CSS pixels. */
  getOverlayHeight: () => Promise<number>;
  /** Subscribe to overlay height changes, rounded to whole non-negative pixels. */
  onOverlayHeight: (callback: (height: number) => void) => Unsubscribe;
}

/** Data backing the title-bar server picker, from the Electron shell. */
export interface ServerPickerInfo {
  /** Origin this window is connected to, e.g. `"http://localhost:8000"`. */
  currentOrigin: string;
  /**
   * The server URL the user picked when sign-in moved to `currentOrigin`'s host
   * (it may carry a workspace `?o=` selector), else null. Absent on older shells.
   */
  currentServer?: string | null;
  /** Recent URL → the server URL the user picked for it, for display. Absent on older shells. */
  recentLabels?: Record<string, string>;
  /**
   * Server URLs supplied through macOS Managed Preferences. Optional because a
   * newer server-served SPA can run inside a desktop shell that predates this.
   */
  managedServers?: string[];
  /** Display names for managed servers, server URL → name. Absent on older shells. */
  managedServerNames?: Record<string, string>;
  /**
   * Whether the shell owns this server's sign-in (Databricks or OIDC browser
   * sign-in) and can sign it out. Absent on older shells.
   */
  canSignOut?: boolean;
  /**
   * Names servers gave themselves in their manifest, origin → name. Display
   * only (a server can call itself anything), so show the host alongside.
   * Absent on older shells.
   */
  serverNames?: Record<string, string>;
  /** Recently-connected server URLs, most recent first. */
  recentServers: string[];
  /**
   * The connected server's version manifest (`/.well-known/omnigent.json`),
   * forwarded by the shell. Optional: shells older than the manifest simply
   * don't send it — the web's `serverManifestOf` supplies the pre-manifest
   * baseline so callers never handle `undefined`.
   */
  serverManifest?: ServerManifest;
}

/**
 * The server's version manifest, as forwarded by the desktop shell from
 * `GET /.well-known/omnigent.json`.
 *
 * Read it through the web's `serverManifestOf` and gate on
 * `manifestVersion >= N`, never `=== N`: a newer server must keep working with
 * an older client, which is the entire point of the document.
 */
export interface ServerManifest {
  /**
   * Envelope version. `0` is the pre-manifest baseline — a server older than
   * the manifest route, or one the shell could not read — so the ordinary
   * `>= 1` gate excludes it without callers testing for null.
   */
  manifestVersion: number;
  /** Installed omnigent package version. Display only, never gate on it. */
  serverVersion: string | null;
  /** Oldest supported desktop build, or null for no floor (the normal case). */
  minDesktopVersion: string | null;
  /**
   * Where server-driven chrome lives. `server_picker` is `"sidebar"` on builds
   * that dock the picker at the sidebar's bottom, `"titlebar"` on older ones.
   * Loosely typed on purpose: unknown keys are the extension point, so a newer
   * server can add shapes this build has never heard of.
   */
  ui: Record<string, unknown>;
}

/** `window.omnigentDesktop`: native integrations for the connected server's page. */
export interface ElectronDesktopBridge {
  /** Discriminator so feature detection is unambiguous. */
  kind: "electron";
  /** Paint the dock/taskbar badge; 0 clears it. Fire-and-forget. */
  setBadgeCount: (count: number) => void;
  /** Fire an OS notification. Resolves true when shown, false otherwise. */
  notify: (params: NativeNotifyParams) => Promise<boolean>;
  /** Subscribe to OS-notification clicks; the callback gets the in-app path. */
  onNotificationActivated: (callback: (path: string) => void) => Unsubscribe;
  /** Subscribe to in-app navigation (native menu actions, same-server deep links). */
  onOpenPath: (callback: (path: string) => void) => Unsubscribe;
  /** Subscribe to the find bar opening (Cmd/Ctrl+F or Edit > Find…). */
  onFindOpened: (callback: () => void) => Unsubscribe;
  /** Subscribe to native menu commands for this window. */
  onDesktopCommand: (callback: (message: DesktopCommandMessage) => void) => Unsubscribe;
  /** Report which menu commands this page can run now (fire-and-forget). */
  setDesktopCommandState: (state: DesktopCommandStateReport) => void;
  /** Close this window once the page has confirmed. Resolves whether it closed. */
  closeWindow: () => Promise<boolean>;
  /** Quit once the page's prompt has confirmed; `dontAskAgain` turns that prompt off. */
  confirmQuit: (options?: { dontAskAgain?: boolean }) => Promise<boolean>;
  /**
   * Start macOS Dictation (Edit > Start Dictation…) in the focused field. Only
   * macOS shells expose it. Resolves whether the shell started it.
   */
  startSystemDictation?: () => Promise<boolean>;
  /** The "Show Omnigent from anywhere" shortcut, with an error while it isn't registered. */
  getGlobalShortcut: () => Promise<GlobalShortcutStatus>;
  /** Saves the shortcut; resolves `{ ok: false, error }` when the shell refuses the chord. */
  setGlobalShortcut: (change?: GlobalShortcutChange) => Promise<GlobalShortcutSaveResult>;
  /** Takes the next chord ahead of the page's shortcuts and the menu; null when cancelled. */
  recordGlobalShortcut: () => Promise<string | null>;
  cancelGlobalShortcutRecording: () => Promise<boolean>;
  /** The runner picked during onboarding for this server, returned once. */
  takeOnboardingRunner: () => Promise<OnboardingRunner | null>;
  /** Server picker data; null off a connected server. */
  getServerPicker: () => Promise<ServerPickerInfo | null>;
  /** Re-point this window to a URL returned by getServerPicker (anything else rejects). */
  switchServer: (url: string) => Promise<void>;
  /** Sign this window's server out; every window on it returns to the setup page. */
  signOutOfServer: () => Promise<boolean>;
  /** Return this window to the bundled "connect to server" setup page. */
  openServerSetup: () => void;
  /** Reveal one of this machine's files in the OS file manager. */
  revealFile: (hostId: string, path: string) => Promise<boolean>;
  /** OS folder chooser on this machine; resolves the chosen path, or null on cancel. */
  chooseDirectory: (defaultPath?: string) => Promise<string | null>;
  /** This machine's path for a dropped File; "" if none or off the pinned server. */
  getPathForFile: (file: File) => Promise<string>;
  /** This machine's identity (CLI installed + host id) — fast, no subprocess. */
  getHostIdentity: () => Promise<HostIdentity | null>;
  /** Start / stop / restart this machine's host daemon for the window's server. */
  controlHost: (action: HostControlAction) => Promise<HostActionResult>;
  /** Desktop feature gates; null off a connected server. */
  getDesktopFeatures: () => Promise<DesktopFeatures | null>;
  /** Connect the user's Arca instance to the window's server as a host. */
  connectArcaHost: () => Promise<ArcaConnectResult>;
  /** Connect a machine the user reaches over SSH; absent on Windows and older shells. */
  connectSshHost?: (destination: string) => Promise<ArcaConnectResult>;
  /** Extend this machine's running Arca instance with an approved mode. */
  extendArca: (mode: ArcaExtendMode) => Promise<ArcaExtendResult>;
  /** Renew this machine's dbcert. */
  refreshDbcert: (options?: DbcertRefreshOptions) => Promise<DbcertRefreshResult>;
  /** Check local credentials before an Arclet launch without starting sign-in. */
  checkDbcert: () => Promise<DbcertStatusResult>;
  /** Subscribe to host status-change pings (re-read on fire); returns an unsubscribe. */
  onHostStatusChanged: (callback: () => void) => Unsubscribe;
  /** The local `omni` CLI status (installed, resolved path, version, source). */
  getCliStatus: () => Promise<CliStatus | null>;
  /** Clear the CLI-path override (revert to auto-detection); resolves status. */
  resetCliPath: () => Promise<CliStatus | null>;
  /** Update preferences and checks; update notices are shell-owned. */
  updates: ElectronDesktopUpdatesBridge;
  /** Report the web app's theme source so native chrome follows it. */
  setColorScheme: (scheme: ThemeSource) => void;

  /**
   * Open or navigate a conversation's embedded browser view; the web treats its
   * presence as the marker for the whole `browser*` suite.
   */
  browserOpenOrNavigate: (
    conversationId: string,
    url: string,
    bounds?: BrowserViewBounds,
    opts?: BrowserOpenOptions,
  ) => Promise<BrowserOpenResult>;
  /** Attach a conversation's view to the window; null detaches everything. */
  browserSetActive: (
    conversationId: string | null,
    options?: { focus?: boolean },
  ) => Promise<BrowserCallResult>;
  /** The attached browser page's view id if it has keyboard focus, else null. */
  browserFocusedView: () => Promise<string | null>;
  /**
   * Hide/show the active embedded browser view while a DOM overlay is open.
   * The native view paints above the renderer, so this is how overlays
   * (dialogs, menus, tooltips, toasts) avoid being covered.
   */
  browserSetSuppressed: (suppressed: boolean) => Promise<BrowserCallResult>;
  /** Reposition the conversation's view to freshly-measured placeholder bounds. */
  browserResize: (conversationId: string, bounds: BrowserViewBounds) => Promise<BrowserCallResult>;
  /** Capture the conversation's view as a base64 PNG data URL. */
  browserScreenshot: (
    conversationId: string,
    opts?: BrowserHostOptions,
  ) => Promise<BrowserScreenshotResult>;
  /** Perform a fixed snapshot, click or type action in the conversation's view. */
  browserPerformAction: (
    conversationId: string,
    action: BrowserAction,
    params?: Record<string, unknown>,
    opts?: BrowserHostOptions,
  ) => Promise<BrowserScriptResult>;
  /** @deprecated Until v0.21: main translates only recognized v0.18 action templates. */
  browserExecute: (
    conversationId: string,
    js: string,
    opts?: BrowserHostOptions,
  ) => Promise<BrowserScriptResult>;
  /** Destroy the conversation's view (explicit close — unmount only detaches). */
  browserClose: (conversationId: string, reason?: string) => Promise<BrowserCloseResult>;
  /** Subscribe to which conversation's view is attached; null when detached. */
  onBrowserHostActiveChanged: (
    callback: (payload: { conversationId: string | null }) => void,
  ) => Unsubscribe;
  /** Forward the recent-session gesture from a focused embedded Browser page. */
  onBrowserRecentSessionInput: (
    callback: (input: BrowserRecentSessionInput) => void,
  ) => Unsubscribe;
  /** Enable native input interception only while this renderer supports it. */
  browserSetRecentSessionSwitchSupported: (supported: boolean) => Promise<BrowserCallResult>;
  /** Subscribe to page links Cmd/Ctrl- or middle-clicked in an embedded browser view. */
  onBrowserOpenTab: (
    callback: (payload: { conversationId: string; url: string }) => void,
  ) => Unsubscribe;
  /** Route modifier-clicked page links to `onBrowserOpenTab` while supported. */
  browserSetOpenTabSupported: (supported: boolean) => Promise<BrowserCallResult>;
  /** Clear native key interception when the recent-session gesture was declined. */
  browserCancelRecentSessionSwitch: () => Promise<BrowserCallResult>;
  /** Subscribe to browser-view creation, including detached views. */
  onBrowserViewCreated: (callback: (payload: { conversationId: string }) => void) => Unsubscribe;
  /** Whether a view already exists for a conversation. */
  browserHasView: (conversationId: string) => Promise<BrowserViewState>;
  /** Subscribe to browser-view close events. */
  onBrowserViewClosed: (
    callback: (payload: { conversationId: string; reason: string | null }) => void,
  ) => Unsubscribe;
  /** Navigate the conversation's view back one history entry (no-op if it can't). */
  browserGoBack: (conversationId: string) => Promise<BrowserNavResult>;
  /** Navigate the conversation's view forward one history entry. */
  browserGoForward: (conversationId: string) => Promise<BrowserNavResult>;
  /** Reload the conversation's view. */
  browserReload: (conversationId: string) => Promise<BrowserCallResult>;
  /** Toggle Chrome DevTools (docked bottom) for the conversation's view. */
  openBrowserDevTools: (conversationId: string) => Promise<BrowserCallResult>;
  /**
   * Inspect the local Google Chrome profiles for cookies in the site tree the
   * conversation's view is on. Metadata only (profile + per-scope counts): no
   * Keychain access and no cookie values.
   */
  browserChromeCookiePreview: (conversationId: string) => Promise<ChromeCookiePreviewResult>;
  /**
   * Copy that site's cookies out of one Chrome profile into the conversation's
   * own in-memory browser session. Decrypts via the Keychain (system prompt on
   * the first import); `expectedHost` must still match the view's current site.
   */
  browserChromeCookieImport: (
    conversationId: string,
    args: ChromeCookieImportArgs,
  ) => Promise<ChromeCookieImportResult>;
  /** Subscribe to URL and title changes, including background views. */
  onBrowserUrlChanged: (callback: (payload: BrowserUrlChange) => void) => Unsubscribe;
  /** Subscribe to back/forward availability for a conversation's view. */
  onBrowserNavState: (callback: (payload: BrowserNavState) => void) => Unsubscribe;
  /** Inject the design-mode picker into the conversation's view. */
  browserEnableDesignMode: (conversationId: string) => Promise<BrowserCallResult>;
  /** Tear the design-mode picker back down. */
  browserDisableDesignMode: (conversationId: string) => Promise<BrowserCallResult>;
  /** Paint a submit's success/failure into the in-page popup. */
  browserSignalDesignResult: (
    conversationId: string,
    result: BrowserDesignResult,
  ) => Promise<BrowserCallResult>;
  /** Subscribe to design-mode element selections. */
  onBrowserElementSelected: (callback: (payload: BrowserElementSelection) => void) => Unsubscribe;
  /** Subscribe to design-mode prompt submits (Send/Enter). */
  onBrowserElementPromptSubmit: (
    callback: (payload: BrowserElementPromptSubmit) => void,
  ) => Unsubscribe;
  /** Subscribe to design-mode prompt dismissals (× / Escape). */
  onBrowserElementPromptDismiss: (
    callback: (payload: { conversationId: string }) => void,
  ) => Unsubscribe;
}

/** Common result of setup-page actions that can fail with a message. */
export interface SetupActionResult {
  ok: boolean;
  error?: string;
  /** Headline of the failure, when the shell split it from `errorAction`. */
  errorMessage?: string;
  /** What the user can do about the failure. */
  errorAction?: string;
}

/** CLI status as the setup page sees it. */
export interface SetupCliStatus extends CliStatus {
  /** Whether this platform can install the CLI from the setup page. */
  installSupported: boolean;
  /** Whether a local server already answers its health check. */
  localServerRunning: boolean;
}

/** `window.omnigentSetup`: the bundled setup page's bridge. */
export interface ElectronSetupBridge {
  /** The saved server URL (its display label when it has one), or null. */
  getServerUrl: () => Promise<string | null>;
  /** Persist and navigate to a server URL. */
  setServerUrl: (
    url: string,
    opts?: { requestId?: string },
  ) => Promise<{ cancelled?: true; reconnecting?: true }>;
  /** Cancel only this setup window's matching connection attempt. */
  cancelServerConnection: (requestId: string) => Promise<boolean>;
  /** Subscribe to connecting/authenticating phases for a request ID. */
  onConnectionProgress: (
    callback: (progress: { requestId?: string; phase: "connecting" | "authenticating" }) => void,
  ) => Unsubscribe;
  /** Organization-provided server URLs from macOS Managed Preferences. */
  getManagedServers: () => Promise<string[]>;
  /** Display names for those servers, server URL → name. */
  getManagedServerNames: () => Promise<Record<string, string>>;
  /** Names servers gave themselves in their manifest, origin → name (display only). */
  getServerNames: () => Promise<Record<string, string>>;
  /** `v2Forced` disables "Switch to legacy" because the env var pins the selector on. */
  getSetupCapabilities: () => Promise<{ v2Forced: boolean; connectedBefore: boolean }>;
  /** Runners the onboarding runner step offers for `url`. */
  getRunnerOptions: (url: string) => Promise<{ remote: boolean; bundledCli: boolean }>;
  /** Connect the onboarding runner to `url`; output streams via onRunnerConnectLog. */
  connectRunner: (
    url: string,
    runner: OnboardingRunner,
  ) => Promise<SetupActionResult & { canceled?: boolean }>;
  /** Subscribe to connectRunner output lines. */
  onRunnerConnectLog: (callback: (line: string) => void) => Unsubscribe;
  /** Live color-scheme override for the wizard; resets on relaunch. */
  setColorScheme: (scheme: ThemeSource) => void;
  /** The session's theme source and the resolved appearance. */
  getColorScheme: () => Promise<{ source: ThemeSource; effective: "light" | "dark" } | null>;
  /** Subscribe to the wizard's effective theme; fires on set and on OS changes. */
  onColorScheme: (callback: (theme: "light" | "dark") => void) => Unsubscribe;
  /** Recently-connected server URLs, most recent first. */
  getRecentServers: () => Promise<string[]>;
  /** Drop one recent server from the saved list; resolves the remaining ones. */
  forgetRecentServer: (url: string) => Promise<string[]>;
  /** Advisory reachability probe for a server URL. Never gates connecting. */
  checkServer: (url: string) => Promise<{ status: "ok" | "reachable" | "unreachable" }>;
  /** Copy text from the bundled setup page to the native clipboard. */
  copyText: (text: string) => Promise<void>;
  /** Toggle the revamped server selector and reload this window to the chosen setup page. */
  setServerSelectorV2: (enabled: boolean) => Promise<void>;
  /** Whether the `omnigent` CLI is installed and runnable. */
  getCliStatus: () => Promise<SetupCliStatus>;
  /** Set an explicit path to the omnigent binary; `accepted` says whether it was saved. */
  setCliPath: (path: string) => Promise<CliStatus>;
  /** Native file picker for the omnigent binary; resolves the path or null. */
  browseCliPath: () => Promise<string | null>;
  /** Start (or reuse) the local server; the caller then connects to `url`. */
  startLocalServer: () => Promise<SetupActionResult & { url?: string; alreadyRunning?: boolean }>;
  /** Subscribe to the local server's startup log lines. */
  onLocalServerSetupLog: (callback: (line: string) => void) => Unsubscribe;
  /** Install the omnigent CLI (macOS); output streams via onCliInstallLog. */
  installCli: () => Promise<SetupActionResult & { installed: boolean }>;
  /** Subscribe to the CLI installer's output lines. */
  onCliInstallLog: (callback: (line: string) => void) => Unsubscribe;
}

/** Each global preload.js exposes in the main window, and its bridge. */
export interface ElectronMainWindowBridges {
  omnigentDesktop: ElectronDesktopBridge;
  omnigentSetup: ElectronSetupBridge;
}

/** Theme the shell resolved for a bundled page. */
export type ResolvedTheme = "light" | "dark";

/** `omnigentDesktop.updates` in the update overlay: full status, update notices included. */
export interface ElectronUpdateOverlayUpdatesBridge {
  getConfig: () => Promise<UpdateConfig>;
  getStatus: () => Promise<UpdateStatus>;
  check: () => Promise<void>;
  download: () => Promise<void>;
  installNow: () => Promise<void>;
  setConfig: (patch: Partial<UpdateConfig>) => Promise<UpdateConfig>;
  onStatus: (callback: (status: UpdateStatus) => void) => Unsubscribe;
}

/** Each global update_overlay_preload exposes, and its bridge. */
export interface ElectronUpdateOverlayBridges {
  /** The shape the web `updateBridge()` reads, so the reused banner works unchanged. */
  omnigentDesktop: { kind: "electron"; updates: ElectronUpdateOverlayUpdatesBridge };
  omnigentUpdateOverlay: {
    /** Report the card height in pixels; 0 hides the overlay. */
    reportHeight: (height: number) => void;
    onTheme: (callback: (theme: ResolvedTheme) => void) => Unsubscribe;
  };
}

/** What the About page shows. */
export interface AboutInfo {
  desktopVersion: string;
  platformName: string;
  /** PNG data URL of the app icon, or null when none could be read. */
  appIconDataUrl: string | null;
  cli: CliStatus;
}

/** `window.omnigentAbout`: the bundled About window's bridge. */
export interface ElectronAboutBridge {
  getInfo: () => Promise<AboutInfo>;
  getDesktopUpdateStatus: () => Promise<UpdateStatus>;
  /** Check now; resolves the status after the check. */
  checkDesktopUpdates: () => Promise<UpdateStatus>;
  downloadDesktopUpdate: () => Promise<void>;
  installDesktopUpdate: () => Promise<void>;
  onDesktopUpdateStatus: (callback: (status: UpdateStatus) => void) => Unsubscribe;
  close: () => void;
}

/** The user's answer to a permission request; "dismiss" grants nothing and keeps the site undecided. */
export type BrowserPermissionChoice = "allow-once" | "always-allow" | "deny" | "dismiss";

/** The request the permission prompt describes. */
export interface BrowserPermissionInfo {
  origin: string;
  /** Whether allowing reloads the page. */
  reload: boolean;
}

/** `window.omnigentBrowserPermission`: the permission prompt's bridge. */
export interface ElectronBrowserPermissionBridge {
  getInfo: () => Promise<BrowserPermissionInfo>;
  choose: (choice: BrowserPermissionChoice) => Promise<void>;
}

/** Find-in-page match counts. */
export interface FindResult {
  active: number;
  matches: number;
}

export interface FindQueryOptions {
  /** Search forward (the default). */
  forward?: boolean;
  /** Step through matches of the current query instead of starting over. */
  findNext?: boolean;
}

/** `window.omnigentFind`: the find bar's bridge. Its listeners cannot be removed. */
export interface ElectronFindBridge {
  /** Run or continue a search; empty text clears the highlight. */
  query: (text: string, opts?: FindQueryOptions) => void;
  close: () => void;
  onResult: (callback: (result: FindResult) => void) => void;
  /** Cmd/Ctrl+F re-activated the open bar. */
  onActivate: (callback: () => void) => void;
}

/** `window.omnigentReconnectOverlay`. */
export interface ElectronReconnectOverlayBridge {
  /** Stop reconnecting and return the window to the setup page. */
  cancel: () => void;
}

/** `window.omnigentReturnBanner`. */
export interface ElectronReturnBannerBridge {
  /** Navigate the parent window back to the remembered server URL. */
  goBack: () => void;
  /** Hide the banner without navigating. */
  dismiss: () => void;
}

/** One workspace the account can sign in to. */
export interface WorkspaceChoice {
  workspaceId: string;
  name: string;
  fqdn: string;
}

/** `window.workspacePicker`: the workspace picker modal's bridge. */
export interface ElectronWorkspacePickerBridge {
  list: () => Promise<WorkspaceChoice[]>;
  choose: (workspaceId: string) => void;
  cancel: () => void;
}

/** The rendered size a connect console reports so the shell can fit its window. */
export interface ConsoleContentSize {
  height: number;
  /** The page's current phase, as the page names it. */
  phase?: string | undefined;
}

/** How a connect console's run ended. */
export interface ConsoleDone {
  ok: boolean;
  error: string | null;
}

export interface ArcaConnectInit {
  serverUrl: string;
  command: string;
  /** Why the console opened (e.g. a preview reconnect), or null. */
  reason: string | null;
}

export interface ArcaConnectStarted {
  /** Whether this command is the sign-in that precedes connecting. */
  login: boolean;
  command: string;
}

/** `window.arcaConnect`: the Arca connect console's bridge. Its listeners cannot be removed. */
export interface ElectronArcaConnectBridge {
  confirm: () => void;
  cancel: () => void;
  resizeToContent: (size: ConsoleContentSize) => void;
  onInit: (callback: (init: ArcaConnectInit) => void) => void;
  onStarted: (callback: (started: ArcaConnectStarted) => void) => void;
  /** A chunk of live stdout/stderr. */
  onOutput: (callback: (text: string) => void) => void;
  onDone: (callback: (done: ConsoleDone) => void) => void;
}

export interface SshConnectInit {
  serverUrl: string;
  destination: string;
  commands: string[];
}

export interface SshConnectStatus {
  step: string;
  code?: string;
}

export interface SshConnectPrompt {
  id: number;
  /** confirm, secret, text or install. */
  kind: string;
  text: string;
}

/** An answer to a prompt, or `cancel: true` to decline it. */
export interface SshConnectAnswer {
  id: number;
  answer?: string;
  cancel?: boolean;
}

/** `window.sshConnect`: the SSH connect console's bridge. Its listeners cannot be removed. */
export interface ElectronSshConnectBridge {
  confirm: () => void;
  cancel: () => void;
  answer: (payload: SshConnectAnswer) => void;
  resizeToContent: (size: ConsoleContentSize) => void;
  onInit: (callback: (init: SshConnectInit) => void) => void;
  onStarted: (callback: (started: Record<string, never>) => void) => void;
  onStatus: (callback: (status: SshConnectStatus) => void) => void;
  onPrompt: (callback: (prompt: SshConnectPrompt) => void) => void;
  /** ssh stopped waiting for that prompt's answer. */
  onPromptClosed: (callback: (closed: { id: number }) => void) => void;
  onOutput: (callback: (text: string) => void) => void;
  onDone: (callback: (done: ConsoleDone) => void) => void;
}
