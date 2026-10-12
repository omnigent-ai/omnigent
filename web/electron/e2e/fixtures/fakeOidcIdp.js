// A minimal OpenID Connect provider for desktop e2e runs: it signs the user in
// at once, issues an RS256 id_token, serves its JWKS, and records every
// authorize request so a test can tell which client reached it.

"use strict";

const crypto = require("node:crypto");
const http = require("node:http");

const b64url = (value) => Buffer.from(value).toString("base64url");

/**
 * @param {{ email?: string, clientId?: string }} [options]
 * @returns {Promise<{ issuer: string, clientId: string, clientSecret: string,
 *   authorizeRequests: Array<{ userAgent: string, url: string }>,
 *   close: () => Promise<void> }>}
 */
async function startFakeOidcIdp({
  email = "desktop-user@example.test",
  clientId = "desktop-e2e",
} = {}) {
  const { privateKey, publicKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
  const kid = "desktop-e2e-key";
  const jwk = { ...publicKey.export({ format: "jwk" }), kid, alg: "RS256", use: "sig" };
  const codes = new Map();
  const authorizeRequests = [];
  let issuer;

  const signIdToken = (claims) => {
    const header = b64url(JSON.stringify({ alg: "RS256", typ: "JWT", kid }));
    const payload = b64url(JSON.stringify(claims));
    const signature = crypto
      .sign("RSA-SHA256", Buffer.from(`${header}.${payload}`), privateKey)
      .toString("base64url");
    return `${header}.${payload}.${signature}`;
  };

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, issuer);
    if (req.method === "GET" && url.pathname === "/authorize") {
      authorizeRequests.push({ userAgent: req.headers["user-agent"] ?? "", url: url.toString() });
      const code = crypto.randomBytes(16).toString("hex");
      codes.set(code, true);
      const back = new URL(url.searchParams.get("redirect_uri"));
      back.searchParams.set("code", code);
      back.searchParams.set("state", url.searchParams.get("state") ?? "");
      res.writeHead(302, { Location: back.toString() });
      res.end();
      return;
    }
    if (req.method === "POST" && url.pathname === "/token") {
      let body = "";
      req.on("data", (chunk) => {
        body += chunk;
      });
      req.on("end", () => {
        const form = new URLSearchParams(body);
        if (!codes.delete(form.get("code"))) {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "invalid_grant" }));
          return;
        }
        const now = Math.floor(Date.now() / 1000);
        const idToken = signIdToken({
          iss: issuer,
          aud: clientId,
          sub: "desktop-e2e-subject",
          email,
          email_verified: true,
          iat: now,
          exp: now + 300,
        });
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({ access_token: "idp-access", token_type: "Bearer", id_token: idToken }),
        );
      });
      return;
    }
    if (req.method === "GET" && url.pathname === "/jwks") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ keys: [jwk] }));
      return;
    }
    res.writeHead(404);
    res.end();
  });

  await new Promise((resolve) => {
    server.listen(0, "127.0.0.1", resolve);
  });
  issuer = `http://127.0.0.1:${server.address().port}`;
  return {
    issuer,
    clientId,
    clientSecret: "desktop-e2e-secret",
    email,
    authorizeRequests,
    close: () =>
      new Promise((resolve) => {
        server.close(() => resolve());
        server.closeAllConnections?.();
      }),
  };
}

/** Server env for an OIDC-mode `omnigent server` signing in at `idp`. */
function oidcServerEnv(idp, serverUrl) {
  return {
    OMNIGENT_AUTH_PROVIDER: "oidc",
    OMNIGENT_OIDC_ISSUER: idp.issuer,
    OMNIGENT_OIDC_CLIENT_ID: idp.clientId,
    OMNIGENT_OIDC_CLIENT_SECRET: idp.clientSecret,
    // Pinned so an ambient setting can't conflict with the client secret.
    OMNIGENT_OIDC_TOKEN_ENDPOINT_AUTH_METHOD: "client_secret_post",
    OMNIGENT_OIDC_REDIRECT_URI: `${serverUrl}/auth/callback`,
    OMNIGENT_OIDC_COOKIE_SECRET: "ab".repeat(32),
    OMNIGENT_OIDC_AUTHORIZATION_ENDPOINT: `${idp.issuer}/authorize`,
    OMNIGENT_OIDC_TOKEN_ENDPOINT: `${idp.issuer}/token`,
    OMNIGENT_OIDC_JWKS_URI: `${idp.issuer}/jwks`,
  };
}

module.exports = { startFakeOidcIdp, oidcServerEnv };
