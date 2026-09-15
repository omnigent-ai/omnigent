import { z } from "zod";
import { getOmnigentServerIdentity } from "./host";
import { getCurrentUserId } from "./identity";

const MAX_AGE_MS = 24 * 60 * 60 * 1000;
const cacheSchema = z.object({ displayName: z.string(), savedAt: z.number() });

export interface SessionModelLabelScope {
  sessionId: string | null;
  hostId: string | null;
  agentId: string | null;
  harness: string | null;
}

export function getSessionModelLabelCacheKey(
  scope: SessionModelLabelScope,
  model: string | null,
): string | null {
  const server = getOmnigentServerIdentity();
  const user = getCurrentUserId();
  if (server === null || user === null || scope.sessionId === null || !model) return null;
  return `omnigent:session-model-label:v1:${JSON.stringify([
    server,
    user,
    scope.sessionId,
    scope.hostId,
    scope.agentId,
    scope.harness,
    model,
  ])}`;
}

export function readSessionModelLabelCache(key: string | null): string | null {
  if (key === null || typeof window === "undefined") return null;
  try {
    const parsed = cacheSchema.safeParse(JSON.parse(window.localStorage.getItem(key) ?? "null"));
    if (!parsed.success) return null;
    const age = Date.now() - parsed.data.savedAt;
    return age >= 0 && age <= MAX_AGE_MS ? parsed.data.displayName : null;
  } catch {
    return null;
  }
}

// Only advertised display names belong here, never model selections or inferred labels.
export function writeSessionModelLabelCache(key: string | null, displayName: string | null): void {
  if (key === null || typeof window === "undefined") return;
  try {
    if (displayName === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, JSON.stringify({ displayName, savedAt: Date.now() }));
  } catch {
    // Private browsing and storage quotas must not affect the live composer.
  }
}
