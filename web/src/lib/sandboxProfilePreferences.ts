// Persisted, app-global preference for the sandbox profile the new-session
// composer starts on, keyed by sandbox provider.
//
// Mirrors readLastSandboxProvider / readLastSandboxRepos: written when a session
// starts, read to seed the profile picker on the next visit. Only the profile id
// is stored. The picker resolves it against the provider's live catalog, so it
// launches on the profile's current revision, and a profile that is no longer
// listed falls back to the provider default.

const STORAGE_KEY = "omnigent:last-sandbox-profile-by-provider";

type ProfileMap = Record<string, string>;

function readMap(): ProfileMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out: ProfileMap = {};
    for (const [provider, id] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof id === "string" && id !== "") out[provider] = id;
    }
    return out;
  } catch {
    return {};
  }
}

/**
 * Read the profile id the user last launched `provider` with, or `null` when
 * nothing is stored, on a server render, or when storage is inaccessible.
 */
export function readLastSandboxProfile(provider: string | null | undefined): string | null {
  if (!provider) return null;
  return readMap()[provider] ?? null;
}

/**
 * Persist `profileId` as the last profile launched on `provider`. Swallows
 * quota/access errors so a failed write can't break session creation.
 */
export function writeLastSandboxProfile(provider: string, profileId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ ...readMap(), [provider]: profileId }),
    );
  } catch {
    // localStorage quota or access errors shouldn't break session creation.
  }
}
