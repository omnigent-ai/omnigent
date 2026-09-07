export const LAST_CREATED_WORKSPACE_STORAGE_KEY = "omnigent:last-created-workspace-by-host";

function readMap(): Record<string, string> {
  if (typeof window === "undefined") return {};
  try {
    const parsed = JSON.parse(
      window.localStorage.getItem(LAST_CREATED_WORKSPACE_STORAGE_KEY) ?? "{}",
    ) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed).filter(
        (entry): entry is [string, string] => typeof entry[1] === "string" && entry[1].length > 0,
      ),
    );
  } catch {
    return {};
  }
}

export function readLastCreatedWorkspace(hostId: string | null): string | null {
  if (!hostId) return null;
  return readMap()[hostId] ?? null;
}

export function writeLastCreatedWorkspace(hostId: string | null, path: string): void {
  if (typeof window === "undefined" || !hostId || path.length === 0) return;
  try {
    window.localStorage.setItem(
      LAST_CREATED_WORKSPACE_STORAGE_KEY,
      JSON.stringify({ ...readMap(), [hostId]: path }),
    );
  } catch {
    // Storage denial must not block a successfully-created session.
  }
}
