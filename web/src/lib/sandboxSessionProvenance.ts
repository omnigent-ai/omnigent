import type { NativeModelOption } from "@/lib/types";

// Per-tab facts about where a session's settings and catalog came from.
const liveCatalogSessions = new Set<string>();
const unsavedLiveCatalogs = new Map<string, readonly NativeModelOption[]>();
const liveCatalogListeners = new Set<() => void>();

function liveCatalogsChanged() {
  for (const listener of liveCatalogListeners) listener();
}

export function subscribeToLiveCatalogs(listener: () => void): () => void {
  liveCatalogListeners.add(listener);
  return () => liveCatalogListeners.delete(listener);
}

/** The session's runner reported these catalog rows while this tab was listening. */
export function markLiveSessionCatalog(
  sessionId: string,
  models: readonly NativeModelOption[],
): void {
  liveCatalogSessions.add(sessionId);
  unsavedLiveCatalogs.set(sessionId, models);
  liveCatalogsChanged();
}

export function hasLiveSessionCatalog(sessionId: string): boolean {
  return liveCatalogSessions.has(sessionId);
}

/** The latest reported rows not yet saved; a snapshot rehydration never adds any. */
export function unsavedLiveCatalog(sessionId: string): readonly NativeModelOption[] | null {
  return unsavedLiveCatalogs.get(sessionId) ?? null;
}

/** Mark these reported rows saved, unless a newer report replaced them. */
export function claimLiveCatalog(sessionId: string, models: readonly NativeModelOption[]): void {
  if (unsavedLiveCatalogs.get(sessionId) !== models) return;
  unsavedLiveCatalogs.delete(sessionId);
  liveCatalogsChanged();
}

export interface SandboxLaunch {
  /** The model picked at launch and its picker label, when it had one. */
  model: string | null;
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
