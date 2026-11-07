// Per-tab facts about where a session's settings and catalog came from.
const liveCatalogSessions = new Set<string>();
const sandboxLaunchesToCheck = new Set<string>();

/** The session's runner reported its model catalog while this tab was listening. */
export function markLiveSessionCatalog(sessionId: string): void {
  liveCatalogSessions.add(sessionId);
}

export function hasLiveSessionCatalog(sessionId: string): boolean {
  return liveCatalogSessions.has(sessionId);
}

/** This tab created the managed-sandbox session, so its launch choices await a check. */
export function markSandboxLaunch(sessionId: string): void {
  sandboxLaunchesToCheck.add(sessionId);
}

/** Claim a pending launch check; returns false when none is pending. */
export function takeSandboxLaunch(sessionId: string): boolean {
  return sandboxLaunchesToCheck.delete(sessionId);
}

export interface LaunchNotice {
  kind: "info" | "error";
  text: string;
}

const launchNotices = new Map<string, LaunchNotice[]>();

/** Hold a launch-check notice until the user is back on its session. */
export function deferLaunchNotice(sessionId: string, notice: LaunchNotice): void {
  launchNotices.set(sessionId, [...(launchNotices.get(sessionId) ?? []), notice]);
}

/** Claim the notices held for a session. */
export function takeLaunchNotices(sessionId: string): LaunchNotice[] {
  const notices = launchNotices.get(sessionId) ?? [];
  launchNotices.delete(sessionId);
  return notices;
}
