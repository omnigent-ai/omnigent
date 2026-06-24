// Bridge between the web app and the optional native shells.
//
// The SAME `ap-web` bundle runs in two places:
//   1. A normal browser tab (served by the Omnigent server).
//   2. Inside the Electron desktop wrapper (`ap-web/electron`), which loads
//      that exact server-served bundle in a Chromium BrowserWindow.
//   3. Inside the iOS wrapper (`ap-web/ios`), which loads the same bundle in
//      a WKWebView.
//
// In native cases we can do better than the Web platform: fire OS-native
// notifications and paint an app badge count via a small injected bridge. In
// case (1) none of that exists, so every function here degrades to a no-op /
// `false` and the caller falls back to the Web Notifications path it already
// has.
//
// Design notes:
//   * Detection is feature-based (an injected `window.omnigentNative` or the
//     legacy Electron `window.omnigentDesktop` object), never a build flag —
//     one bundle, multiple runtimes, decided at runtime.
//   * This module never throws: a broken/old shell must not take down
//     notifications in the browser path.

/**
 * Phase of a native sidebar-drag gesture (see `onSidebarDrag`). `begin` and
 * `move` are live drag frames carrying an open fraction; `open` and `close`
 * are the settle decision the shell made on release.
 */
export type SidebarDragPhase = "begin" | "move" | "open" | "close";

/**
 * Minimal API surface exposed by native shells. Electron exposes the legacy
 * `window.omnigentDesktop`; newer shells expose `window.omnigentNative`.
 * Kept intentionally tiny and string/number only so it survives bridge
 * serialization.
 */
interface NativeShellApi {
  /** Discriminator so feature detection is unambiguous. */
  kind: "electron" | "ios";
  /** Paint the dock/taskbar badge; 0 clears it. */
  setBadgeCount: (count: number) => void;
  /** Fire an OS notification; resolves true when it was shown. */
  notify: (params: NativeNotifyParams) => Promise<boolean>;
  // Optional: a shell older than this SPA may lack notification-click routing,
  // in which case clicking a native toast only focuses the app (the prior
  // behavior) instead of also navigating.
  /**
   * Subscribe to OS-notification clicks. The main process sends the in-app
   * path the notification carried (its `navigatePath`); returns an unsubscribe.
   */
  onNotificationActivated?: (callback: (path: string) => void) => () => void;
  /**
   * Subscribe to native sidebar-drag events. The iOS shell streams a left-edge
   * swipe here (the gesture it repurposed from back-navigation) so the renderer
   * can drive its sidebar as an interactive drawer: `begin`/`move` carry a 0→1
   * open fraction the sidebar should track live (no transition), and
   * `open`/`close` are the settle decision on release (animate to that resting
   * state). Returns an unsubscribe.
   */
  onSidebarDrag?: (callback: (phase: SidebarDragPhase, progress: number) => void) => () => void;
  /**
   * Let native chrome react to web UI state. The iOS shell uses this to show
   * its floating server switcher only when the chat transcript is visible.
   */
  setServerSwitcherHidden?: (hidden: boolean) => void;
  /**
   * Legacy iOS bridge name from the sidebar-only implementation. Kept as a
   * fallback so a newer SPA can still ask an older shell to hide the switcher.
   */
  setSidebarOpen?: (open: boolean) => void;
  /**
   * Drive the native Chat/Terminal switcher (iOS). The web app owns the truth
   * and pushes the current mode, whether the terminal is reachable / booting,
   * and whether the switcher should be shown at all. Absent on older shells,
   * in which case the web renders its own in-page pill instead.
   */
  setViewMode?: (params: NativeViewModeParams) => void;
  /** Subscribe to taps on the native switcher; returns an unsubscribe. */
  onViewModeChanged?: (callback: (mode: NativeViewMode) => void) => () => void;
}

export type NativeViewMode = "chat" | "terminal";

export interface NativeViewModeParams {
  /** Currently selected view. */
  mode: NativeViewMode;
  /** Whether the Terminal option is selectable (a reachable PTY exists). */
  terminalEnabled: boolean;
  /** Terminal is booting but not yet openable — drives a spinner. */
  terminalStartingUp?: boolean;
  /** Whether the switcher should be shown at all right now. */
  visible: boolean;
}

/**
 * Electron-specific bridge. The server-picker trio is optional: the SPA is
 * server-served and may be newer than the installed shell, whose preload then
 * lacks these methods.
 */
interface ElectronDesktopApi extends NativeShellApi {
  kind: "electron";
  /** Current server origin + recent servers, or null on a foreign page. */
  getServerPicker?: () => Promise<ServerPickerInfo | null>;
  /** Re-point this window to a previously-connected server URL. */
  switchServer?: (url: string) => Promise<void>;
  /** Return this window to the shell's "connect to server" setup page. */
  openServerSetup?: () => void;
}

/** Data backing the title-bar server picker, from the Electron shell. */
export interface ServerPickerInfo {
  /** Origin this window is connected to, e.g. `"http://localhost:8000"`. */
  currentOrigin: string;
  /** Recently-connected server URLs, most recent first. */
  recentServers: string[];
}

/** The Electron preload bridge, or undefined outside the Electron shell. */
function electronApi(): ElectronDesktopApi | undefined {
  if (typeof window === "undefined") return undefined;
  const api = (window as unknown as { omnigentDesktop?: ElectronDesktopApi }).omnigentDesktop;
  return api?.kind === "electron" ? api : undefined;
}

/** The native shell bridge, or undefined outside any native shell. */
function nativeApi(): NativeShellApi | undefined {
  if (typeof window === "undefined") return undefined;
  const api = (window as unknown as { omnigentNative?: NativeShellApi }).omnigentNative;
  if (api?.kind === "ios" || api?.kind === "electron") return api;
  return electronApi();
}

/** True when running inside the Electron desktop shell. */
export function isElectronShell(): boolean {
  return electronApi() !== undefined;
}

/**
 * True when running inside the Electron desktop shell on macOS — the one
 * platform where the shell hides the native title bar (titleBarStyle
 * "hiddenInset") and the web layer must reserve space for the traffic
 * lights and supply a window-drag strip (see the `[data-electron-mac]`
 * rules in index.css).
 */
export function isMacElectronShell(): boolean {
  return isElectronShell() && navigator.userAgent.includes("Macintosh");
}

/** True when running inside the iOS WKWebView native shell. */
export function isIOSShell(): boolean {
  return nativeApi()?.kind === "ios";
}

/**
 * True when running inside the native desktop shell (Electron).
 *
 * The shell loads the same server-served SPA in a Chromium webview, so the
 * web code can do better than the Web platform: OS notifications and a
 * dock/taskbar badge. Detection is feature-based — the Electron preload
 * exposes `window.omnigentDesktop` — never a build flag. In a plain browser
 * this is false and every native call here degrades to a no-op / web fallback.
 */
export function isNativeShell(): boolean {
  return nativeApi() !== undefined;
}

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

/**
 * Show an OS-native notification via the Electron preload bridge (which calls
 * the main-process `Notification` API and wires click-to-focus on its side).
 *
 * Returns `true` when the notification was handed to the bridge, `false` when
 * not running under Electron or anything went wrong (so the caller can fall
 * back to the Web Notifications API).
 */
export async function nativeNotify({
  title,
  body,
  navigatePath,
}: NativeNotifyParams): Promise<boolean> {
  const native = nativeApi();
  if (!native) return false;
  try {
    return await native.notify({ title, body, navigatePath });
  } catch (err) {
    // Only reachable inside a native shell. Log rather than swallow so a
    // broken bridge is visible instead of silently dropping notifications.
    console.warn("[nativeBridge] native notify failed:", err);
    return false;
  }
}

/**
 * Subscribe to native notification clicks from the desktop shell. The shell
 * fires the in-app path the clicked notification carried (its `navigatePath`),
 * so the renderer can route to it — restoring the in-browser behavior where
 * clicking a toast opens its conversation.
 *
 * Returns an unsubscribe function. A no-op (returning a no-op unsubscribe)
 * outside the Electron shell or under a shell too old to support click
 * routing, so callers can register it unconditionally.
 */
export function onNativeNotificationActivated(callback: (path: string) => void): () => void {
  const native = nativeApi();
  if (!native?.onNotificationActivated) return () => {};
  try {
    return native.onNotificationActivated(callback);
  } catch (err) {
    console.warn("[nativeBridge] native onNotificationActivated failed:", err);
    return () => {};
  }
}

/**
 * Subscribe to native sidebar-drag events from the iOS shell's left-edge swipe
 * (the gesture it repurposed from back-navigation), so the renderer can drive
 * its sidebar as an interactive drawer — tracking the finger on `begin`/`move`
 * and animating to the settled state on `open`/`close`.
 *
 * Returns an unsubscribe function. A no-op (returning a no-op unsubscribe)
 * outside a native shell or under a shell too old to support the gesture, so
 * callers can register it unconditionally.
 */
export function onNativeSidebarDrag(
  callback: (phase: SidebarDragPhase, progress: number) => void,
): () => void {
  const native = nativeApi();
  if (!native?.onSidebarDrag) return () => {};
  try {
    return native.onSidebarDrag(callback);
  } catch (err) {
    console.warn("[nativeBridge] native onSidebarDrag failed:", err);
    return () => {};
  }
}

/**
 * Paint the dock / taskbar badge with a count (macOS dock badge, Linux Unity
 * launcher count). Pass `0` (or omit) to clear it.
 *
 * No-op outside the Electron shell. The Electron main process calls
 * `app.setBadgeCount`, which on Windows is unsupported at the app level — we
 * intentionally don't paper over that.
 */
export async function setBadgeCount(count: number): Promise<void> {
  const native = nativeApi();
  if (!native) return;
  try {
    native.setBadgeCount(count);
  } catch (err) {
    console.warn("[nativeBridge] native setBadgeCount failed:", err);
  }
}

/**
 * Inform a native shell that its server switcher should hide. Older shells
 * simply lack this optional method, so this degrades to a no-op.
 */
export function setNativeServerSwitcherHidden(hidden: boolean): void {
  const native = nativeApi();
  const setter = native?.setServerSwitcherHidden ?? native?.setSidebarOpen;
  if (!setter) return;
  try {
    setter(hidden);
  } catch (err) {
    console.warn("[nativeBridge] native setServerSwitcherHidden failed:", err);
  }
}

/** @deprecated Use setNativeServerSwitcherHidden. */
export function setNativeSidebarOpen(open: boolean): void {
  setNativeServerSwitcherHidden(open);
}

/**
 * Push the current Chat/Terminal state to the native switcher (iOS). The web
 * app owns this state; the native bar is a thin control surface that renders it
 * and reports taps back via {@link onNativeViewModeChanged}. No-op on shells
 * without the native switcher (older iOS shells, Electron, plain browser) — the
 * caller renders its own in-page pill there.
 */
export function setNativeViewMode(params: NativeViewModeParams): void {
  const native = nativeApi();
  if (!native?.setViewMode) return;
  try {
    native.setViewMode(params);
  } catch (err) {
    console.warn("[nativeBridge] native setViewMode failed:", err);
  }
}

/**
 * Subscribe to taps on the native Chat/Terminal switcher. The shell sends the
 * mode the user selected; route it into the web view's own state. Returns an
 * unsubscribe; a no-op outside a shell that exposes the native switcher.
 */
export function onNativeViewModeChanged(callback: (mode: NativeViewMode) => void): () => void {
  const native = nativeApi();
  if (!native?.onViewModeChanged) return () => {};
  try {
    return native.onViewModeChanged(callback);
  } catch (err) {
    console.warn("[nativeBridge] native onViewModeChanged failed:", err);
    return () => {};
  }
}

/**
 * Fetch the title-bar server picker data from the Electron shell: the
 * window's current server origin plus the recently-connected server list.
 *
 * Resolves `null` outside the Electron shell, under a shell too old to
 * support the picker, or on a page the shell doesn't recognize as a
 * connected server — callers hide the picker in all of those cases.
 */
export async function getServerPicker(): Promise<ServerPickerInfo | null> {
  const electron = electronApi();
  if (!electron?.getServerPicker) return null;
  try {
    return await electron.getServerPicker();
  } catch (err) {
    console.warn("[nativeBridge] electron getServerPicker failed:", err);
    return null;
  }
}

/**
 * Ask the Electron shell to re-point this window to another
 * previously-connected server URL (one of `ServerPickerInfo.recentServers`).
 * The shell navigates the whole window, so on success this page unloads.
 */
export async function switchServer(url: string): Promise<void> {
  const electron = electronApi();
  if (!electron?.switchServer) return;
  try {
    await electron.switchServer(url);
  } catch (err) {
    console.warn("[nativeBridge] electron switchServer failed:", err);
  }
}

/**
 * Ask the Electron shell to return this window to its "connect to server"
 * setup page (the picker's "+ Connect to new server…" action). The window
 * navigates away on success.
 */
export function openServerSetup(): void {
  const electron = electronApi();
  if (!electron?.openServerSetup) return;
  try {
    electron.openServerSetup();
  } catch (err) {
    console.warn("[nativeBridge] electron openServerSetup failed:", err);
  }
}
