// Per-agent preference for the last sandbox repos (and branches), written on
// create and read by the landing to seed the picker; keying by agent keeps one
// agent's remembered repos from riding into another agent's launch.

const STORAGE_KEY = "omnigent:last-sandbox-repos";
// Single-repo key written by builds before multi-repo; purged on every read or write.
const LEGACY_STORAGE_KEY = "omnigent:last-sandbox-repo";

/**
 * Strip any userinfo (`user[:secret]@`) from an http(s) URL, so a pasted
 * tokenized clone URL (e.g. `https://x-access-token:PAT@github.com/o/r`) is
 * never persisted to localStorage as a secret at rest. Non-http(s) URLs
 * (e.g. `git@github.com:o/r`) are left unchanged.
 */
function stripUrlUserinfo(url: string): string {
  return url.replace(/^(https?:\/\/)[^/@]*@/i, "$1");
}

/** A repo the user launched with: its clone URL and branch (may be ""). */
export interface LastSandboxRepo {
  /** Repo URL, e.g. ``https://github.com/org/repo.git`` (never blank). */
  url: string;
  /** Branch name, or ``""`` for the repo's default. */
  branch: string;
}

/** Trim + userinfo-strip one stored entry, or ``null`` when its URL is blank. */
function normalizeEntry(value: unknown): LastSandboxRepo | null {
  if (typeof value !== "object" || value === null) return null;
  const url = stripUrlUserinfo(String((value as { url?: unknown }).url ?? "").trim());
  const branch = String((value as { branch?: unknown }).branch ?? "").trim();
  return url === "" ? null : { url, branch };
}

// The stored agent-id → repos map; ``{}`` when nothing is stored or the value is
// malformed. Older builds stored one browser-wide array whose agent is unknown,
// so it is ignored rather than attributed to anyone.
function readMap(): Record<string, unknown> {
  try {
    window.localStorage.removeItem(LEGACY_STORAGE_KEY);
  } catch {
    // A blocked purge must not prevent reading the current preference.
  }
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

/**
 * Repos ``agentId`` last launched a sandbox with; ``[]`` when none, no agent, or storage is unusable.
 */
export function readLastSandboxRepos(agentId: string | null | undefined): LastSandboxRepo[] {
  if (typeof window === "undefined" || !agentId) return [];
  const entry = readMap()[agentId];
  if (!Array.isArray(entry)) return [];
  return entry.map(normalizeEntry).filter((r): r is LastSandboxRepo => r !== null);
}

/**
 * Store ``repos`` as ``agentId``'s last launch (blank URLs dropped, empty clears it); never throws.
 */
export function writeLastSandboxRepos(
  agentId: string | null | undefined,
  repos: LastSandboxRepo[],
): void {
  if (typeof window === "undefined" || !agentId) return;
  try {
    const cleaned = repos
      .map((r) => ({ url: stripUrlUserinfo(r.url.trim()), branch: r.branch.trim() }))
      .filter((r) => r.url !== "");
    // Read and write run in one synchronous call; a concurrent tab can at worst
    // lose a preference.
    const { [agentId]: _previous, ...others } = readMap();
    const map = cleaned.length === 0 ? others : { ...others, [agentId]: cleaned };
    if (Object.keys(map).length === 0) {
      window.localStorage.removeItem(STORAGE_KEY);
      return;
    }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // localStorage quota or access errors shouldn't break session creation.
  }
}
