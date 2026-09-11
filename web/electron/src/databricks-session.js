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

const SESSION_CREATE_PATH = "/auth/session/create";

/**
 * Ensure ``ses`` holds a live DBAUTH cookie for ``origin``: get (or refresh, or
 * interactively mint) an access token, then exchange it for the cookie.
 *
 * @param {Electron.Session} ses The session whose cookie jar to seed.
 * @param {string} origin e.g. ``"https://ws.databricks.com"``.
 * @param {{ interactive?: boolean, nextPath?: string, forceLogin?: boolean }} [opts]
 * @returns {Promise<void>}
 */
async function ensureDatabricksSession(
  ses,
  origin,
  { interactive = true, nextPath = "/omnigent", forceLogin = false } = {},
) {
  const accessToken = await getValidAccessToken(origin, { interactive, forceLogin });
  await mintSessionCookie(ses, origin, accessToken, nextPath);
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
    // Default redirect mode = follow: Electron processes the 302 (storing its
    // Set-Cookie in `ses`) before landing on next_url.
    const request = net.request({ method: "GET", url, session: ses });
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
