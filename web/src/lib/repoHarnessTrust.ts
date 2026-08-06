/**
 * Client-side trust ledger for repo-declared harness commands.
 *
 * A `.omnigent/config.yaml` checked into a repo can declare arbitrary shell
 * commands, so the first launch of a discovered agent always goes through an
 * explicit consent dialog. "Always allow" persists a grant keyed by
 * (host, workspace, slug, SHA-256 of the exact command) — any change to the
 * command re-prompts. Per-browser only; server-side trust is a follow-up.
 */

const STORAGE_KEY = "omnigent.repoHarnessTrust";

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function grantKey(
  hostId: string,
  workspace: string,
  slug: string,
  command: string,
): Promise<string> {
  return `${hostId}|${workspace}|${slug}|${await sha256Hex(command)}`;
}

function readGrants(): Record<string, true> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? (parsed as Record<string, true>) : {};
  } catch {
    return {};
  }
}

/** Whether this exact command was previously granted "always allow". */
export async function isRepoCommandTrusted(
  hostId: string,
  workspace: string,
  slug: string,
  command: string,
): Promise<boolean> {
  return readGrants()[await grantKey(hostId, workspace, slug, command)] === true;
}

/** Persist an "always allow" grant for this exact command. */
export async function trustRepoCommand(
  hostId: string,
  workspace: string,
  slug: string,
  command: string,
): Promise<void> {
  const grants = readGrants();
  grants[await grantKey(hostId, workspace, slug, command)] = true;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(grants));
  } catch {
    // Quota/private-mode failures just mean re-prompting next time.
  }
}

/** SHA-256 hex of raw bundle bytes — the identity a bundle grant is keyed on. */
export async function digestBundleBytes(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function digestKey(hostId: string, workspace: string, slug: string, digestHex: string): string {
  return `${hostId}|${workspace}|${slug}|${digestHex}`;
}

/** Whether this exact bundle digest was previously granted "always allow". */
export function isRepoDigestTrusted(
  hostId: string,
  workspace: string,
  slug: string,
  digestHex: string,
): boolean {
  return readGrants()[digestKey(hostId, workspace, slug, digestHex)] === true;
}

/** Persist an "always allow" grant for this exact bundle digest. */
export function trustRepoDigest(
  hostId: string,
  workspace: string,
  slug: string,
  digestHex: string,
): void {
  const grants = readGrants();
  grants[digestKey(hostId, workspace, slug, digestHex)] = true;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(grants));
  } catch {
    // Quota/private-mode failures just mean re-prompting next time.
  }
}
