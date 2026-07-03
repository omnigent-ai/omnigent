// Persisted, app-global preference for which brain harness override the
// new-session landing composer starts on, keyed by agent id.
//
// Bundle agents (e.g. Polly, Debby) let the user pick a brain harness
// (claude-sdk, openai-agents, …) that overrides the agent spec's default.
// This store remembers the last pick per agent so returning users start on
// the harness they used last. A stale value (harness removed server-side)
// is sent as `harness_override` and rejected by the server at create time.

const STORAGE_KEY = "omnigent:last-harness-by-agent";

type HarnessMap = Record<string, string>;

function readMap(): HarnessMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out: HarnessMap = {};
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v === "string") out[k] = v;
    }
    return out;
  } catch {
    return {};
  }
}

/**
 * Read the last brain-harness override for `agentId`. Returns `null` when
 * nothing is stored, on a server render, or when storage is inaccessible.
 */
export function readLastHarness(agentId: string | null | undefined): string | null {
  if (!agentId) return null;
  return readMap()[agentId] ?? null;
}

/**
 * Persist `harness` as the user's last brain-harness pick for `agentId`.
 * Pass `null` to clear the override (the agent will use its spec default).
 */
export function writeLastHarness(agentId: string | null | undefined, harness: string | null): void {
  if (typeof window === "undefined" || !agentId) return;
  try {
    const map = readMap();
    if (harness === null) {
      delete map[agentId];
    } else {
      map[agentId] = harness;
    }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // localStorage quota or access errors shouldn't break the composer.
  }
}
