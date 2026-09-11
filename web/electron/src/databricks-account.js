// Account workspaces lookup for SPOG (account-first) login.
//
// An account-scoped token can't hit /auth/session/create on the account/SPOG
// host (the edge serves the account console SPA there). DB One resolves a
// workspace FQDN from the account workspaces API and bridges against that host
// instead. Mirrors genie-one-desktop/src/main/accountWorkspaces.ts.

"use strict";

/** Decode a JWT's `iss` claim. No verification — the server verifies on use;
 * we read only the public issuer to route the follow-up account API call. */
function decodeJwtIssuer(token) {
  const parts = String(token).split(".");
  if (parts.length < 2) return null;
  try {
    const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const payload = JSON.parse(Buffer.from(base64, "base64").toString("utf8"));
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
 *
 * @param {string} accessToken
 * @returns {{ accountOrigin: string, accountId: string } | null}
 */
function parseAccountFromToken(accessToken) {
  const iss = decodeJwtIssuer(accessToken);
  if (!iss) return null;
  const m = /^(https:\/\/[^/]+)\/oidc\/accounts\/([^/]+)\/?$/.exec(iss);
  return m ? { accountOrigin: m[1], accountId: m[2] } : null;
}

/**
 * List the account's RUNNING workspaces via
 * ``GET {accountOrigin}/api/2.0/accounts/{id}/workspaces`` with the account
 * bearer. Returns the id / name / fqdn the picker and bridge need.
 *
 * @param {{ accountOrigin: string, accountId: string }} account
 * @param {string} accessToken
 * @returns {Promise<Array<{ workspaceId: string, name: string, fqdn: string }>>}
 */
async function listRunningWorkspaces(account, accessToken) {
  const url = `${account.accountOrigin}/api/2.0/accounts/${account.accountId}/workspaces`;
  const resp = await fetch(url, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
  });
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    throw new Error(`account workspaces lookup failed: ${resp.status} ${url} ${body.slice(0, 200)}`);
  }
  const body = await resp.json();
  // The API returns a bare array; accept an object wrapper defensively.
  const rows = Array.isArray(body) ? body : (body?.workspaces ?? []);
  return rows
    .filter((w) => w.workspace_status === "RUNNING" && typeof w.workspace_fqdn === "string")
    .map((w) => ({
      workspaceId: String(w.workspace_id),
      name: w.workspace_name ?? w.workspace_fqdn,
      fqdn: w.workspace_fqdn,
    }));
}

module.exports = { parseAccountFromToken, listRunningWorkspaces };
