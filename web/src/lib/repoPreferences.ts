// Persisted, app-global preference for the last GitHub repos (and branches) the
// user launched a sandbox session with.
//
// Mirrors baseBranchPreferences: the landing composer reads this to seed the
// repo list when there's no in-session draft, so returning users don't re-pick
// the same repos every time. The stored URLs are the source of truth; the repo
// combobox derives its selection from them, so a stored repo the account can no
// longer access simply shows unselected (no stale entry is forced into the
// picker). Written on session create.

const STORAGE_KEY = "omnigent:last-sandbox-repos";
// Single-repo key written by builds before multi-repo; read once for migration.
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

/**
 * Read the last repos the user launched a sandbox with: the stored list, or
 * ``[]`` when nothing is stored, on a server render (no ``window``), when
 * storage is inaccessible, or when the stored value is malformed — never
 * throws. Trims on read so a hand-edited or stale entry can't seed an
 * un-normalized value. Falls back once to the single-repo key an older build
 * wrote, so a returning user's last repo still seeds the picker.
 */
export function readLastSandboxRepos(): LastSandboxRepo[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as unknown;
      if (!Array.isArray(parsed)) return [];
      return parsed.map(normalizeEntry).filter((r): r is LastSandboxRepo => r !== null);
    }
    const legacy = window.localStorage.getItem(LEGACY_STORAGE_KEY);
    if (legacy) {
      const one = normalizeEntry(JSON.parse(legacy) as unknown);
      return one ? [one] : [];
    }
    return [];
  } catch {
    return [];
  }
}

/**
 * Persist ``repos`` as the last repos launched with. Entries with a blank
 * (or whitespace-only) URL are dropped; an empty result clears the preference.
 * Swallows quota/access errors so a failed write can't break session creation.
 */
export function writeLastSandboxRepos(repos: LastSandboxRepo[]): void {
  if (typeof window === "undefined") return;
  try {
    const cleaned = repos
      .map((r) => ({ url: stripUrlUserinfo(r.url.trim()), branch: r.branch.trim() }))
      .filter((r) => r.url !== "");
    if (cleaned.length === 0) {
      window.localStorage.removeItem(STORAGE_KEY);
      return;
    }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(cleaned));
  } catch {
    // localStorage quota or access errors shouldn't break session creation.
  }
}
