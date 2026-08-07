/**
 * Client-side trust ledger for repo-declared agents.
 *
 * Repo content is arbitrary code from a clone, so the first launch of a
 * discovered agent always goes through an explicit consent dialog.
 * "Always allow" persists a grant keyed by a JSON-encoded tagged tuple —
 * `["cmd", host, workspace, slug, sha256(command)]` for ACP commands,
 * `["bundle", host, workspace, slug, sha256(bundle bytes)]` for agent
 * configs — so the two grant types can never collide, path characters
 * can't be confused for delimiters, and any change to the command or
 * bundle content re-prompts. Per-browser only; server-side trust is a
 * follow-up.
 */

const STORAGE_KEY = "omnigent.repoHarnessTrust";

async function sha256Hex(data: string | ArrayBuffer): Promise<string> {
  const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/** SHA-256 hex of raw bundle bytes — the identity a bundle grant is keyed on. */
export async function digestBundleBytes(bytes: ArrayBuffer): Promise<string> {
  return sha256Hex(bytes);
}

function grantKey(
  kind: "cmd" | "bundle",
  hostId: string,
  workspace: string,
  slug: string,
  digestHex: string,
): string {
  return JSON.stringify([kind, hostId, workspace, slug, digestHex]);
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

function writeGrant(key: string): void {
  const grants = readGrants();
  grants[key] = true;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(grants));
  } catch {
    // Quota/private-mode failures just mean re-prompting next time.
  }
}

/** Whether this exact command was previously granted "always allow". */
export async function isRepoCommandTrusted(
  hostId: string,
  workspace: string,
  slug: string,
  command: string,
): Promise<boolean> {
  const key = grantKey("cmd", hostId, workspace, slug, await sha256Hex(command));
  return readGrants()[key] === true;
}

/** Persist an "always allow" grant for this exact command. */
export async function trustRepoCommand(
  hostId: string,
  workspace: string,
  slug: string,
  command: string,
): Promise<void> {
  writeGrant(grantKey("cmd", hostId, workspace, slug, await sha256Hex(command)));
}

/** Whether this exact bundle digest was previously granted "always allow". */
export async function isRepoDigestTrusted(
  hostId: string,
  workspace: string,
  slug: string,
  digestHex: string,
): Promise<boolean> {
  return readGrants()[grantKey("bundle", hostId, workspace, slug, digestHex)] === true;
}

/** Persist an "always allow" grant for this exact bundle digest. */
export async function trustRepoDigest(
  hostId: string,
  workspace: string,
  slug: string,
  digestHex: string,
): Promise<void> {
  writeGrant(grantKey("bundle", hostId, workspace, slug, digestHex));
}
