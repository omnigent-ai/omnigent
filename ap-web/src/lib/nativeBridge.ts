// Bridge between the web app and the optional Electron desktop shell.
//
// The SAME `ap-web` bundle runs in two places:
//   1. A normal browser tab (served by the Omnigent server).
//   2. Inside the Electron desktop wrapper (`ap-web/electron`), which loads
//      that exact server-served bundle in a Chromium BrowserWindow.
//
// In case (2) we can do better than the Web platform: fire OS-native desktop
// notifications and paint a dock / taskbar badge count, both via the Electron
// preload bridge exposed on `window.omnigentDesktop`. In case (1) none of
// that exists, so every function here degrades to a no-op / `false` and the
// caller falls back to the Web Notifications path it already has.
//
// Design notes:
//   * Detection is feature-based (the preload's `window.omnigentDesktop`
//     object with `kind: "electron"`), never a build flag — one bundle, two
//     runtimes, decided at runtime.
//   * This module never throws: a broken/old shell must not take down
//     notifications in the browser path.

/**
 * Minimal API surface exposed by the Electron preload on
 * `window.omnigentDesktop`. The Electron shell (`ap-web/electron`) wraps the
 * server-served SPA; its preload bridges to the main process over IPC for the
 * two OS integrations we need: dock/taskbar badge and OS notifications. Kept
 * intentionally tiny and string/number only so it survives `contextBridge`
 * serialization.
 */
interface ElectronDesktopApi {
  /** Discriminator so feature detection is unambiguous. */
  kind: "electron";
  /** Paint the dock/taskbar badge; 0 clears it. */
  setBadgeCount: (count: number) => void;
  /** Fire an OS notification; resolves true when it was shown. */
  notify: (params: NativeNotifyParams) => Promise<boolean>;
  // Optional: a shell older than this SPA may lack notification-click routing,
  // in which case clicking a desktop toast only focuses the window (the prior
  // behavior) instead of also navigating.
  /**
   * Subscribe to OS-notification clicks. The main process sends the in-app
   * path the notification carried (its `navigatePath`); returns an unsubscribe.
   */
  onNotificationActivated?: (callback: (path: string) => void) => () => void;
  // The server-picker trio is optional: the SPA is server-served and may be
  // newer than the installed shell, whose preload then lacks these methods.
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
  return isElectronShell();
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
  const electron = electronApi();
  if (!electron) return false;
  try {
    return await electron.notify({ title, body, navigatePath });
  } catch (err) {
    // Only reachable inside the desktop shell. Log rather than swallow so a
    // broken bridge is visible instead of silently dropping notifications.
    console.warn("[nativeBridge] electron notify failed:", err);
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
  const electron = electronApi();
  if (!electron?.onNotificationActivated) return () => {};
  try {
    return electron.onNotificationActivated(callback);
  } catch (err) {
    console.warn("[nativeBridge] electron onNotificationActivated failed:", err);
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
  const electron = electronApi();
  if (!electron) return;
  try {
    electron.setBadgeCount(count);
  } catch (err) {
    console.warn("[nativeBridge] electron setBadgeCount failed:", err);
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
