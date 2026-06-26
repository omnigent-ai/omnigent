/**
 * Helpers for classifying the server URL that serves standalone ap-web.
 *
 * Sharing a session from a loopback-only server produces links nobody else can
 * open, so the UI disables the Share affordance when the current server origin
 * is local.
 */

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"]);

export function isLocalServerOrigin(origin: string): boolean {
  try {
    const { hostname } = new URL(origin);
    return LOOPBACK_HOSTS.has(hostname);
  } catch {
    return false;
  }
}

export function isCurrentServerLocal(): boolean {
  if (typeof window === "undefined") return false;
  return isLocalServerOrigin(window.location.origin);
}
