// Browser preference shared with managed deployments; stores service names, never credentials.
const STORAGE_KEY = "omnigent:last-mcp-services";

export function readLastMcpServices(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((v): v is string => typeof v === "string");
  } catch {
    return [];
  }
}

export function writeLastMcpServices(names: readonly string[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([...names]));
  } catch {
    // Storage quota or access errors must not break the composer.
  }
}
