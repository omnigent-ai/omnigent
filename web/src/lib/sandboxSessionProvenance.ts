// Per-tab facts about where a session's settings and catalog came from.
const liveCatalogSessions = new Set<string>();

/** The session's runner reported its model catalog while this tab was listening. */
export function markLiveSessionCatalog(sessionId: string): void {
  liveCatalogSessions.add(sessionId);
}

export function hasLiveSessionCatalog(sessionId: string): boolean {
  return liveCatalogSessions.has(sessionId);
}

export interface SandboxLaunch {
  /** Display name of the model picked at launch, when the picker had one. */
  modelLabel: string | null;
}

const sandboxLaunchesToCheck = new Map<string, SandboxLaunch>();

/** This tab created the managed-sandbox session, so its launch choices await a check. */
export function markSandboxLaunch(sessionId: string, launch: SandboxLaunch): void {
  sandboxLaunchesToCheck.set(sessionId, launch);
}

/** Claim a pending launch check; returns null when none is pending. */
export function takeSandboxLaunch(sessionId: string): SandboxLaunch | null {
  const launch = sandboxLaunchesToCheck.get(sessionId) ?? null;
  sandboxLaunchesToCheck.delete(sessionId);
  return launch;
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
