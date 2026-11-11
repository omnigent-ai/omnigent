/** Server label holding the SHA-256 of a worktree session's canonical root. */
export const WORKTREE_ROOT_LABEL_KEY = "omnigent.git.worktree_root_sha256";

const WINDOWS_ABSOLUTE_PATH = /^(?:[a-zA-Z]:[\\/]|[\\/]{2}[^\\/]+[\\/]+[^\\/]+)/;

/**
 * Hex SHA-256 the server records for a worktree root, e.g. for
 * `"/repo/.worktrees/login"`. Windows paths compare case- and separator-insensitively.
 */
export async function worktreeRootFingerprint(path: string): Promise<string> {
  const normalized = WINDOWS_ABSOLUTE_PATH.test(path)
    ? path.replaceAll("\\", "/").toLowerCase()
    : path;
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(normalized.replace(/\/+$/, "")),
  );
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** The path itself, then each enclosing directory, nearest first. */
function selfAndAncestors(path: string): string[] {
  const windows = WINDOWS_ABSOLUTE_PATH.test(path);
  const isSeparator = (char: string) => char === "/" || (windows && char === "\\");
  const candidates = [path];
  for (let index = path.length - 1; index >= 0; index--) {
    if (isSeparator(path[index]!)) candidates.push(path.slice(0, index) || path.slice(0, 1));
  }
  return candidates;
}

/**
 * Worktree root the server removes for a session directory, which may be a
 * subdirectory of it. Mirrors the server's `recorded_worktree_root`: no
 * fingerprint means the directory is the root. Returns null when unresolvable.
 */
export async function recordedWorktreeRoot(
  workspace: string,
  fingerprint: string | undefined,
): Promise<string | null> {
  if (fingerprint === undefined) return workspace;
  if (typeof crypto === "undefined" || crypto.subtle === undefined) return null;
  const candidates = selfAndAncestors(workspace);
  try {
    const fingerprints = await Promise.all(candidates.map(worktreeRootFingerprint));
    return candidates[fingerprints.indexOf(fingerprint)] ?? null;
  } catch {
    return null;
  }
}
