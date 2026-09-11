// Bridge a Databricks OAuth access token into a DBAUTH web-session cookie.
//
// Calls the login service's /auth/session/create (the OAuth→session bridge from
// the Databricks One mobile design) with a bearer token; that endpoint mints an
// SMv2 session and returns it as a Set-Cookie: DBAUTH on a 302. We land that
// cookie in the target window's Electron session so the /omnigent SPA opens
// already authenticated. databricks-oauth.js supplies/refreshes the token;
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
 * @param {{ interactive?: boolean, nextPath?: string }} [opts]
 * @returns {Promise<void>}
 */
async function ensureDatabricksSession(ses, origin, { interactive = true, nextPath = "/omnigent" } = {}) {
  const accessToken = await getValidAccessToken(origin, { interactive });
  await mintSessionCookie(ses, origin, accessToken, nextPath);
}

/**
 * GET /auth/session/create with a bearer token and store the returned DBAUTH
 * cookie in ``ses``. Uses redirect:"manual" so we capture the 302 (and its
 * Set-Cookie) without following it to next_url. In manual mode Electron delivers
 * a 3xx via the "redirect" event (NOT "response"); we read the cookie there and
 * abort. A 200 (no redirect) is handled on "response". Rejects on a missing
 * DBAUTH — surfacing, e.g., the 403 you'd get if the endpoint's SAFE allowlist
 * doesn't include this client_id.
 */
function mintSessionCookie(ses, origin, accessToken, nextPath) {
  return new Promise((resolve, reject) => {
    const url = `${origin}${SESSION_CREATE_PATH}?next_url=${encodeURIComponent(nextPath)}`;
    const request = net.request({ method: "GET", url, session: ses, redirect: "manual" });
    request.setHeader("Authorization", `Bearer ${accessToken}`);
    let captured = false;

    // Resolve if a DBAUTH cookie is present — either parsed from the response's
    // Set-Cookie, or (Electron can strip Set-Cookie from exposed headers while
    // still storing it) already in the session jar. Otherwise throw, including
    // the redirect target so a bounce to /login/sso is obvious.
    async function finish(status, setCookies, locationNote) {
      const dbauth = setCookies.map(parseSetCookie).find((c) => c && c.name === "DBAUTH");
      if (dbauth) {
        await applyCookie(ses, origin, dbauth);
        console.log(`[omnigent] databricks session: DBAUTH cookie set for ${origin} (via HTTP ${status})`);
        return;
      }
      const jar = await ses.cookies.get({ url: origin, name: "DBAUTH" });
      if (jar.length > 0) {
        console.log(`[omnigent] databricks session: DBAUTH already in session jar (HTTP ${status})`);
        return;
      }
      throw new Error(`${SESSION_CREATE_PATH} HTTP ${status}${locationNote} — no DBAUTH cookie set`);
    }

    request.on("redirect", (statusCode, _method, redirectUrl, responseHeaders) => {
      captured = true;
      request.abort(); // we only want the Set-Cookie, not the redirect target
      console.log(
        `[omnigent] databricks session: ${SESSION_CREATE_PATH} -> HTTP ${statusCode}, Location=${redirectUrl}`,
      );
      finish(statusCode, headerValues(responseHeaders, "set-cookie"), ` (Location=${redirectUrl})`).then(
        resolve,
        reject,
      );
    });

    // A 200 (no redirect) carries the Set-Cookie on the response itself.
    request.on("response", (response) => {
      const status = response.statusCode;
      const setCookies = headerValues(response.headers, "set-cookie");
      response.on("data", () => {});
      response.on("end", () => {
        finish(status, setCookies, "").then(resolve, reject);
      });
    });

    request.on("error", (err) => {
      if (captured) return; // abort() after capturing the redirect lands here — expected
      reject(err);
    });
    request.end();
  });
}

/** Normalize an Electron response header (string or string[]) to a string[]. */
function headerValues(headers, name) {
  const v = headers?.[name] ?? headers?.[name.toLowerCase()];
  if (Array.isArray(v)) return v;
  if (typeof v === "string") return [v];
  return [];
}

/** Parse one Set-Cookie header value into {name, value, ...attributes}. */
function parseSetCookie(raw) {
  if (typeof raw !== "string" || raw === "") return null;
  const [nameValue, ...attrs] = raw.split(";");
  const eq = nameValue.indexOf("=");
  if (eq < 0) return null;
  const cookie = { name: nameValue.slice(0, eq).trim(), value: nameValue.slice(eq + 1).trim() };
  for (const attr of attrs) {
    const [k, ...rest] = attr.split("=");
    const key = k.trim().toLowerCase();
    const val = rest.join("=").trim();
    if (key === "path") cookie.path = val;
    else if (key === "domain") cookie.domain = val;
    else if (key === "secure") cookie.secure = true;
    else if (key === "httponly") cookie.httpOnly = true;
    else if (key === "samesite") cookie.sameSite = val.toLowerCase();
    else if (key === "max-age") {
      const n = Number(val);
      if (!Number.isNaN(n)) cookie.maxAge = n;
    } else if (key === "expires") cookie.expires = val;
  }
  return cookie;
}

/** Map an RFC SameSite value to Electron's cookies.set enum. */
function mapSameSite(s) {
  switch (s) {
    case "none":
      return "no_restriction";
    case "lax":
      return "lax";
    case "strict":
      return "strict";
    default:
      return "unspecified";
  }
}

/** Seconds-since-epoch expiry from Max-Age/Expires, or undefined (session cookie). */
function cookieExpiry(c) {
  if (typeof c.maxAge === "number") return Math.floor(Date.now() / 1000) + c.maxAge;
  if (c.expires) {
    const t = Date.parse(c.expires);
    if (!Number.isNaN(t)) return Math.floor(t / 1000);
  }
  return undefined;
}

async function applyCookie(ses, origin, c) {
  const details = {
    url: origin,
    name: c.name,
    value: c.value,
    path: c.path || "/",
    secure: c.secure ?? origin.startsWith("https:"),
    httpOnly: c.httpOnly ?? true,
    sameSite: mapSameSite(c.sameSite),
  };
  // Honor an explicit Domain; otherwise leave it host-only for the origin.
  if (c.domain) details.domain = c.domain;
  const exp = cookieExpiry(c);
  if (exp) details.expirationDate = exp;
  await ses.cookies.set(details);
}

module.exports = {
  databricksOAuthConfigured,
  ensureDatabricksSession,
};
