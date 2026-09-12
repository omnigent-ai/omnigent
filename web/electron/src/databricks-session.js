// Bridge a Databricks OAuth access token into a DBAUTH web-session cookie.
//
// Calls the login service's /auth/session/create (the OAuth→session bridge from
// the Databricks One mobile design) with a bearer token; that endpoint mints an
// SMv2 session and returns it as a 302 -> next_url with Set-Cookie: DBAUTH. We
// let net.request FOLLOW that redirect so Electron commits the cookie into the
// target window's session jar, then confirm it landed, so the /omnigent SPA
// opens already authenticated. databricks-oauth.js supplies/refreshes the token;
// main.js calls ensureDatabricksSession at the pre-load and session-expiry seams.

"use strict";

const { net } = require("electron");
const {
  databricksOAuthConfigured,
  getValidAccessToken,
  isTrustedDatabricksOrigin,
} = require("./databricks-oauth");
const { parseAccountFromToken, listRunningWorkspaces } = require("./databricks-account");

const SESSION_CREATE_PATH = "/auth/session/create";
// Bound on the session-create request so a stalled socket can't hang connect.
const NETWORK_TIMEOUT_MS = 20_000;

/**
 * Ensure ``ses`` holds a live DBAUTH cookie for ``origin``: get (or refresh, or
 * interactively mint) an access token, then exchange it for the cookie.
 *
 * @param {Electron.Session} ses The session whose cookie jar to seed.
 * @param {string} origin e.g. ``"https://ws.databricks.com"``.
 * @param {{ interactive?: boolean, nextPath?: string,
 *   pickWorkspace?: (workspaces: Array<{workspaceId: string, name: string, fqdn: string}>)
 *     => Promise<{fqdn: string, name: string} | null> }} [opts]
 *   ``pickWorkspace`` is required for account-scoped (SPOG) logins — it chooses
 *   which workspace to bridge the account token to.
 * @returns {Promise<string>} The workspace origin the session was created for
 *   (may differ from ``origin`` in SPOG mode — the user's picked workspace).
 */
async function ensureDatabricksSession(
  ses,
  origin,
  { interactive = true, nextPath = "/omnigent", pickWorkspace } = {},
) {
  // `issuerOrigin` is where the token was minted (its refresh/exchange host).
  const { accessToken, workspaceOrigin: issuerOrigin } = await getValidAccessToken(origin, {
    interactive,
  });

  // Decide which workspace host to bridge against. A workspace-scoped token
  // bridges against its own origin. An ACCOUNT-scoped token (SPOG / account
  // entry) cannot — the account host has no /auth/session/create — so resolve the
  // account's workspaces, let the caller pick one, and bridge the account token
  // against that workspace's FQDN. Mirrors DB One.
  let bridgeOrigin = issuerOrigin;
  const account = parseAccountFromToken(accessToken);
  if (account) {
    const workspaces = await listRunningWorkspaces(account, accessToken);
    if (workspaces.length === 0) {
      throw new Error("no running workspaces available for this account");
    }
    if (typeof pickWorkspace !== "function") {
      throw new Error("account-scoped login requires a workspace picker");
    }
    const picked = await pickWorkspace(workspaces);
    if (!picked) throw new Error("workspace selection cancelled");
    bridgeOrigin = `https://${picked.fqdn}`;
    console.log(`[omnigent] databricks session: bridging to workspace ${bridgeOrigin}`);
  }

  // Never send the bearer to a non-Databricks host. bridgeOrigin is either the
  // token issuer (already validated) or a workspace FQDN from the account API
  // (validated here) — gate it before the session-create call carries the token.
  if (!isTrustedDatabricksOrigin(bridgeOrigin)) {
    throw new Error(`refusing to send credentials to untrusted workspace origin: ${bridgeOrigin}`);
  }

  await mintSessionCookie(ses, bridgeOrigin, accessToken, nextPath);
  return bridgeOrigin;
}

/**
 * Exchange the bearer token for a DBAUTH cookie via /auth/session/create. The
 * endpoint replies 302 -> next_url with Set-Cookie: DBAUTH; we let net.request
 * follow the redirect so Electron commits that cookie into ``ses`` (Electron
 * hides Set-Cookie from the JS-visible redirect headers, so we read the jar
 * rather than parse the header), then confirm DBAUTH is present.
 */
async function mintSessionCookie(ses, origin, accessToken, nextPath) {
  const status = await new Promise((resolve, reject) => {
    const url = `${origin}${SESSION_CREATE_PATH}?next_url=${encodeURIComponent(nextPath)}`;
    // useSessionCookies:true is required for the response's Set-Cookie to be
    // stored in `ses` (it defaults to false — session:ses alone won't persist
    // cookies). Default redirect mode = follow, so Electron processes the 302
    // (committing DBAUTH into the jar) before landing on next_url.
    const request = net.request({ method: "GET", url, session: ses, useSessionCookies: true });
    request.setHeader("Authorization", `Bearer ${accessToken}`);
    const timer = setTimeout(() => request.abort(), NETWORK_TIMEOUT_MS);
    request.on("response", (response) => {
      response.on("data", () => {});
      response.on("end", () => {
        clearTimeout(timer);
        resolve(response.statusCode);
      });
    });
    request.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    request.end();
  });

  // A successful bridge follows the 302 to next_url and ends < 400. An error
  // (e.g. 401/403 when the endpoint isn't enabled for this account) ends >= 400
  // and must NOT be reported as success just because a stale DBAUTH lingers.
  if (status >= 400) {
    throw new Error(`${SESSION_CREATE_PATH} returned HTTP ${status}`);
  }
  const jar = await ses.cookies.get({ url: origin, name: "DBAUTH" });
  if (jar.length === 0) {
    throw new Error(
      `${SESSION_CREATE_PATH}: no DBAUTH cookie stored in the session (final HTTP ${status})`,
    );
  }
  console.log(`[omnigent] databricks session: signed in to ${origin}`);
}

module.exports = {
  databricksOAuthConfigured,
  ensureDatabricksSession,
};
