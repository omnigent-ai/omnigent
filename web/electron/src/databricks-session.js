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
const { databricksOAuthConfigured, getValidAccessToken } = require("./databricks-oauth");
const { parseAccountFromToken, listRunningWorkspaces } = require("./databricks-account");

const SESSION_CREATE_PATH = "/auth/session/create";

/**
 * Ensure ``ses`` holds a live DBAUTH cookie for ``origin``: get (or refresh, or
 * interactively mint) an access token, then exchange it for the cookie.
 *
 * @param {Electron.Session} ses The session whose cookie jar to seed.
 * @param {string} origin e.g. ``"https://ws.databricks.com"``.
 * @param {{ interactive?: boolean, nextPath?: string, forceLogin?: boolean,
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
  { interactive = true, nextPath = "/omnigent", forceLogin = false, pickWorkspace } = {},
) {
  // `issuerOrigin` is where the token was minted (its refresh/exchange host).
  const { accessToken, workspaceOrigin: issuerOrigin } = await getValidAccessToken(origin, {
    interactive,
    forceLogin,
  });

  // Decide which workspace host to bridge against. A workspace-scoped token
  // bridges against its own origin. An ACCOUNT-scoped token (SPOG / account
  // entry) cannot — the account host has no /auth/session/create — so resolve a
  // workspace from the account workspaces API and let the caller pick one, then
  // bridge the account token against that workspace's FQDN. Mirrors DB One.
  let bridgeOrigin = issuerOrigin;
  const account = parseAccountFromToken(accessToken);
  if (account) {
    console.log(
      `[omnigent] databricks session: account-scoped token (account ${account.accountId} @ ` +
        `${account.accountOrigin}); resolving a workspace to bridge`,
    );
    const workspaces = await listRunningWorkspaces(account, accessToken);
    console.log(
      `[omnigent] databricks session: account has ${workspaces.length} running workspace(s): ` +
        workspaces.map((w) => w.fqdn).join(", "),
    );
    if (workspaces.length === 0) {
      throw new Error("no running workspaces available for this account");
    }
    if (typeof pickWorkspace !== "function") {
      throw new Error("account-scoped login requires a workspace picker");
    }
    const picked = await pickWorkspace(workspaces);
    if (!picked) throw new Error("workspace selection cancelled");
    bridgeOrigin = `https://${picked.fqdn}`;
    console.log(`[omnigent] databricks session: picked workspace ${picked.name} -> ${bridgeOrigin}`);
  } else if (issuerOrigin !== origin) {
    console.log(`[omnigent] databricks session: workspace-scoped token; entered ${origin} -> ${issuerOrigin}`);
  }

  await mintSessionCookie(ses, bridgeOrigin, accessToken, nextPath);
  return bridgeOrigin;
}

/**
 * Remove every cookie the given workspace origin would send. Strict test mode
 * uses this to start unauthenticated so a stale workspace session can't silently
 * sign the window in.
 *
 * @param {Electron.Session} ses
 * @param {string} origin
 * @returns {Promise<void>}
 */
async function clearWorkspaceCookies(ses, origin) {
  const cookies = await ses.cookies.get({ url: origin });
  await Promise.all(
    cookies.map((c) => {
      const scheme = c.secure ? "https" : "http";
      const host =
        c.domain && c.domain.startsWith(".") ? c.domain.slice(1) : c.domain || new URL(origin).hostname;
      return ses.cookies.remove(`${scheme}://${host}${c.path || "/"}`, c.name);
    }),
  );
}

/**
 * Exchange the bearer token for a DBAUTH cookie via /auth/session/create. The
 * endpoint replies 302 -> next_url with Set-Cookie: DBAUTH; we let net.request
 * follow the redirect so Electron commits that cookie into ``ses`` (Electron
 * hides Set-Cookie from the JS-visible redirect headers, so we read the jar
 * rather than parse the header), then confirm DBAUTH is present.
 */
async function mintSessionCookie(ses, origin, accessToken, nextPath) {
  // Diagnostic: same token against an ordinary API. 200 => token valid + scoped.
  try {
    const probe = await fetch(`${origin}/api/2.0/preview/scim/v2/Me`, {
      headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
      redirect: "manual",
    });
    console.log(
      `[omnigent] databricks session: token probe /scim/v2/Me -> status=${probe.status} type=${probe.type}`,
    );
  } catch (e) {
    console.warn("[omnigent] databricks session: token probe failed:", e.message);
  }

  const status = await new Promise((resolve, reject) => {
    const url = `${origin}${SESSION_CREATE_PATH}?next_url=${encodeURIComponent(nextPath)}`;
    // useSessionCookies:true is required for the response's Set-Cookie to be
    // stored in `ses` (it defaults to false — session:ses alone won't persist
    // cookies). Default redirect mode = follow, so Electron processes the 302
    // (committing DBAUTH into the jar) before landing on next_url.
    const request = net.request({ method: "GET", url, session: ses, useSessionCookies: true });
    request.setHeader("Authorization", `Bearer ${accessToken}`);
    request.on("response", (response) => {
      response.on("data", () => {});
      response.on("end", () => resolve(response.statusCode));
    });
    request.on("error", reject);
    request.end();
  });

  const jar = await ses.cookies.get({ url: origin, name: "DBAUTH" });
  if (jar.length === 0) {
    throw new Error(`${SESSION_CREATE_PATH}: no DBAUTH cookie stored in the session (final HTTP ${status})`);
  }
  console.log(`[omnigent] databricks session: DBAUTH cookie set for ${origin} (final HTTP ${status})`);
}

module.exports = {
  databricksOAuthConfigured,
  ensureDatabricksSession,
  clearWorkspaceCookies,
};
