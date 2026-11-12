// Account workspaces lookup for SPOG (account-first) login.
//
// An account-scoped token can't hit /auth/session/create on the account/SPOG
// host (the edge serves the account console SPA there). DB One resolves a
// workspace FQDN from the account workspaces API and bridges against that host
// instead. Mirrors genie-one-desktop/src/main/accountWorkspaces.ts.

import { isTrustedDatabricksOrigin } from "./databricks-oauth.ts";

/** One row of the account workspaces API, untrusted. */
interface WorkspaceRow {
  workspace_id?: unknown;
  workspace_name?: unknown;
  workspace_fqdn?: unknown;
  workspace_status?: unknown;
}

// Bound on the account-workspaces lookup so a stalled socket can't hang connect.
const NETWORK_TIMEOUT_MS = 20_000;

/** The account an account-scoped token was issued for. */
export interface DatabricksAccount {
  accountOrigin: string;
  accountId: string;
}

/** A running workspace the picker offers and the bridge targets. */
export interface RunningWorkspace {
  workspaceId: string;
  name: string;
  fqdn: string;
}

/** Decode a JWT's `iss` claim. No verification — the server verifies on use;
 * we read only the public issuer to route the follow-up account API call. */
function decodeJwtIssuer(token: unknown): string | null {
  const parts = String(token).split(".");
  if (parts.length < 2) return null;
  try {
    const base64 = parts[1]!.replace(/-/g, "+").replace(/_/g, "/");
    // A null payload throws on `.iss`, which reads as no issuer.
    const payload = JSON.parse(Buffer.from(base64, "base64").toString("utf8")) as {
      iss?: unknown;
    };
    return typeof payload.iss === "string" ? payload.iss : null;
  } catch {
    return null;
  }
}

/**
 * Parse the account origin + id from an account-scoped token's issuer claim
 * (``https://accounts.<cloud>.databricks.com/oidc/accounts/<accountId>``).
 * Returns null for a workspace-scoped token (its issuer is a workspace host) —
 * the signal to bridge against the token's own origin, no account hop.
 */
export function parseAccountFromToken(accessToken: string): DatabricksAccount | null {
  const iss = decodeJwtIssuer(accessToken);
  if (!iss) return null;
  const m = /^(https:\/\/[^/]+)\/oidc\/accounts\/([^/]+)\/?$/.exec(iss);
  return m ? { accountOrigin: m[1]!, accountId: m[2]! } : null;
}

/**
 * List the account's RUNNING workspaces via
 * ``GET {accountOrigin}/api/2.0/accounts/{id}/workspaces`` with the account
 * bearer. Returns the id / name / fqdn the picker and bridge need.
 */
export async function listRunningWorkspaces(
  account: DatabricksAccount,
  accessToken: string,
  { signal }: { signal?: AbortSignal | undefined } = {},
): Promise<RunningWorkspace[]> {
  signal?.throwIfAborted();
  // Never send the bearer to a non-Databricks host. accountOrigin comes from the
  // token's own `iss`, but that claim isn't verified here, so gate it.
  if (!isTrustedDatabricksOrigin(account.accountOrigin)) {
    throw new Error(
      `refusing to send credentials to untrusted account origin: ${account.accountOrigin}`,
    );
  }
  const url = `${account.accountOrigin}/api/2.0/accounts/${account.accountId}/workspaces`;
  const resp = await fetch(url, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
    // This API answers with JSON; a 3xx is a login bounce, not a destination to
    // carry the bearer to. Fail below instead of following it.
    redirect: "manual",
    signal: signal
      ? AbortSignal.any([signal, AbortSignal.timeout(NETWORK_TIMEOUT_MS)])
      : AbortSignal.timeout(NETWORK_TIMEOUT_MS),
  });
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    signal?.throwIfAborted();
    throw new Error(
      `account workspaces lookup failed: ${resp.status} ${url} ${body.slice(0, 200)}`,
    );
  }
  const body: unknown = await resp.json();
  signal?.throwIfAborted();
  // The API returns a bare array; accept an object wrapper defensively.
  const rows: unknown = Array.isArray(body)
    ? body
    : ((body as { workspaces?: unknown } | null | undefined)?.workspaces ?? []);
  // Rows are read as the API documents them; a non-array `rows` or a null row throws here.
  return (rows as WorkspaceRow[])
    .filter(
      (w): w is WorkspaceRow & { workspace_fqdn: string } =>
        w.workspace_status === "RUNNING" && typeof w.workspace_fqdn === "string",
    )
    .map((w) => ({
      workspaceId: String(w.workspace_id),
      // `workspace_name` is not type-checked; a non-string passes through.
      name: (w.workspace_name ?? w.workspace_fqdn) as string,
      fqdn: w.workspace_fqdn,
    }));
}
