// Thin wrapper around the Web Notifications API.
//
// Kept separate from the transition logic in useIdleNotifications so the
// "when do we notify" decision stays testable without touching the global
// Notification constructor, and so the one place that calls `new
// Notification` / `requestPermission` is small and feature-detected.
//
// We deliberately use the page-scoped Notification API rather than
// ServiceWorkerRegistration.showNotification(): the service worker we register
// is installability/update-only and intercepts nothing.

// When running inside the Electron desktop shell, notifications are routed
// through the OS-native notification API (via the preload bridge) instead of
// the Web Notifications API. `nativeNotify` returns false in a plain browser,
// so the web path below remains the default everywhere else.
import { isNativeShell, nativeNotify } from "@/lib/nativeBridge";

export function isNotificationSupported(): boolean {
  // Check the constructor is actually present, not just that the key
  // exists — a stubbed-to-undefined `window.Notification` still satisfies
  // `"Notification" in window` but can't be dereferenced.
  return typeof window !== "undefined" && typeof window.Notification === "function";
}

/** Current grant state, or null when the API isn't available. */
export function getNotificationPermission(): NotificationPermission | null {
  if (!isNotificationSupported()) return null;
  return Notification.permission;
}

/**
 * Prompt for permission. Must be called from within a user gesture or
 * browsers downgrade the prompt to "quiet UI" (Chrome) or reject it.
 *
 * Safari historically only supported the callback form of
 * `requestPermission`; we feature-detect by function arity (the callback
 * form declares one argument) and adapt it to a Promise.
 */
export async function requestNotificationPermission(): Promise<NotificationPermission | null> {
  if (!isNotificationSupported()) return null;
  if (Notification.requestPermission.length === 1) {
    return new Promise((resolve) => {
      Notification.requestPermission((perm) => resolve(perm));
    });
  }
  return Notification.requestPermission();
}

export interface ShowNotificationParams {
  /** Headline — the conversation's display label. */
  title: string;
  /** Secondary line, e.g. "Agent is ready for your input". */
  body?: string;
  /** Dedupe key; a repeat with the same tag replaces the prior toast. */
  tag?: string;
  /** Invoked when the user clicks the notification (after focusing). */
  onClick?: () => void;
  /**
   * In-app path to open when the notification is clicked, e.g. `"/c/abc"`.
   * The browser path runs `onClick` directly; the Electron path can't carry a
   * JS closure across the process boundary, so it forwards this string to the
   * shell, which routes to it on click (see `onNativeNotificationActivated`).
   */
  navigatePath?: string;
}

/**
 * Show a notification, or no-op when unsupported or not yet granted.
 *
 * Clicking focuses the originating window, runs `onClick` (navigation),
 * and closes the toast. Returns the created Notification (for tests) or
 * null when nothing was shown.
 */
export function showNotification({
  title,
  body,
  tag,
  onClick,
  navigatePath,
}: ShowNotificationParams): Notification | null {
  // Desktop shell: hand off to the native OS notification and skip the web
  // toast entirely. `nativeNotify` is async/best-effort; we don't await it
  // (callers treat this function as fire-and-forget) and return null because
  // no web `Notification` object is created in the native path. We forward
  // `navigatePath` (not the `onClick` closure, which can't cross the IPC
  // boundary) so the shell can route to the conversation on click.
  if (isNativeShell()) {
    void nativeNotify({ title, body, navigatePath });
    return null;
  }
  if (!isNotificationSupported() || Notification.permission !== "granted") return null;
  const notification = new Notification(title, { body, tag });
  notification.onclick = () => {
    window.focus();
    onClick?.();
    notification.close();
  };
  return notification;
}
