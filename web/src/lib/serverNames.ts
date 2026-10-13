/**
 * The name a server gave itself, looked up by the origin of `url`, or null.
 * Display only: a server can call itself anything, so show its host too.
 *
 * @param serverNames Origin → name, from the desktop shell.
 * @param url Any URL on the server.
 */
export function ownServerName(
  serverNames: Record<string, string> | undefined,
  url: string,
): string | null {
  if (!serverNames) return null;
  try {
    const origin = new URL(url).origin;
    return Object.hasOwn(serverNames, origin) ? serverNames[origin] : null;
  } catch {
    return null;
  }
}
