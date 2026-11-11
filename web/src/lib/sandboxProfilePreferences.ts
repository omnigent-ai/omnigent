// Remembers the last picked profile id per provider for the new-session composer.

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
 * Read the profile id the user last picked for `provider`, or `null` when
 * nothing is stored, on a server render, or when storage is inaccessible.
 */
export function readLastSandboxProfile(provider: string | null | undefined): string | null {
  if (!provider) return null;
  return readMap()[provider] ?? null;
}

/**
 * Persist `profileId` as the last profile picked for `provider`. Swallows
 * quota/access errors so a failed write can't break the composer.
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
