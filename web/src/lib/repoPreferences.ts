// Persisted, app-global preference for the last GitHub repo (and branch) the
// user launched a sandbox session with.
//
// Mirrors baseBranchPreferences: the landing composer reads this to seed the
// repo URL + branch fields when there's no in-session draft, so returning users
// don't re-pick the same repo every time. The stored URL is the source of
// truth the free-text field shows; the repo combobox derives its selection from
// it, so a stored repo the account can no longer access simply shows unselected
// (no stale entry is forced into the picker). Written on session create.

const STORAGE_KEY = "omnigent:last-sandbox-repo";

/**
 * Strip any userinfo (`user[:secret]@`) from an http(s) URL, so a pasted
 * tokenized clone URL (e.g. `https://x-access-token:PAT@github.com/o/r`) is
 * never persisted to localStorage as a secret at rest. Non-http(s) URLs
 * (e.g. `git@github.com:o/r`) are left unchanged.
 */
function stripUrlUserinfo(url: string): string {
  return url.replace(/^(https?:\/\/)[^/@]*@/i, "$1");
}

/** The last repo the user launched with: its clone URL and branch (may be ""). */
export interface LastSandboxRepo {
  /** Repo URL, e.g. ``https://github.com/org/repo.git`` (never blank). */
  url: string;
  /** Branch name, or ``""`` for the repo's default. */
  branch: string;
}

/**
 * Read the last repo the user launched a sandbox with: the stored
 * ``{url, branch}``, or ``null`` when nothing is stored, on a server render (no
 * ``window``), when storage is inaccessible, or when the stored value is
 * malformed / has a blank URL — never throws. Trims on read so a hand-edited or
 * stale entry can't seed an un-normalized value.
 */
export function readLastSandboxRepo(): LastSandboxRepo | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (typeof parsed !== "object" || parsed === null) return null;
    const url = stripUrlUserinfo(String((parsed as { url?: unknown }).url ?? "").trim());
    const branch = String((parsed as { branch?: unknown }).branch ?? "").trim();
    return url === "" ? null : { url, branch };
  } catch {
    return null;
  }
}

/**
 * Persist ``url`` (+ optional ``branch``) as the last repo launched with. A
 * blank (or whitespace-only) URL clears the preference. Swallows quota/access
 * errors so a failed write can't break session creation.
 */
export function writeLastSandboxRepo(url: string, branch: string): void {
  if (typeof window === "undefined") return;
  try {
    const trimmedUrl = stripUrlUserinfo(url.trim());
    if (trimmedUrl === "") {
      window.localStorage.removeItem(STORAGE_KEY);
      return;
    }
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ url: trimmedUrl, branch: branch.trim() }),
    );
  } catch {
    // localStorage quota or access errors shouldn't break session creation.
  }
}
