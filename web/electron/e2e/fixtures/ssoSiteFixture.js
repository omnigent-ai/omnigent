// A workspace that signs in through a separate identity provider host, both on
// one loopback server routed by Host: workspace.localhost and idp.localhost.
"use strict";

const http = require("node:http");

const WORKSPACE_SESSION = "workspace_session";

const SECURITY_KEY_PAGE = `<!doctype html><title>Sign In</title>
<h2>Verify with Security Key or Biometric Authenticator</h2>
<p id="status">You will be prompted to use a security key or biometric verification.</p>
<script>
window.verifyState = { outcome: "idle" };
setTimeout(async () => {
  window.verifyState.outcome = "pending";
  try {
    await navigator.credentials.get({ publicKey: {
      challenge: crypto.getRandomValues(new Uint8Array(32)),
      allowCredentials: [{ type: "public-key", id: crypto.getRandomValues(new Uint8Array(16)) }],
      userVerification: "preferred",
      timeout: 120000,
    } });
    window.verifyState.outcome = "signed-in";
  } catch (error) {
    window.verifyState.outcome = error.name;
  }
}, 500);
</script>`;

function parseCookies(header = "") {
  return Object.fromEntries(
    header
      .split(";")
      .map((part) => part.trim())
      .filter(Boolean)
      .map((part) => [part.slice(0, part.indexOf("=")), part.slice(part.indexOf("=") + 1)]),
  );
}

/**
 * @param {{ workspaceSessions: string[] }} accepted
 *   Session cookie values the workspace treats as signed in.
 */
async function startSsoSites({ workspaceSessions }) {
  const requests = [];
  const server = http.createServer((request, response) => {
    const host = (request.headers.host ?? "").split(":")[0];
    const url = new URL(request.url, `http://${request.headers.host}`);
    const cookies = parseCookies(request.headers.cookie);
    requests.push({ host, path: url.pathname, cookieNames: Object.keys(cookies).sort() });
    const send = (status, headers, body = "") => {
      response.writeHead(status, { "Cache-Control": "no-store", ...headers });
      response.end(body);
    };
    if (host === "workspace.localhost") {
      if (workspaceSessions.includes(cookies[WORKSPACE_SESSION])) {
        return send(
          200,
          { "Content-Type": "text/html" },
          "<!doctype html><title>Workspace</title><h1>Signed in to the workspace</h1>",
        );
      }
      const callback = `http://workspace.localhost:${port}/callback`;
      return send(302, {
        Location: `http://idp.localhost:${port}/authorize?redirect_uri=${encodeURIComponent(callback)}`,
      });
    }
    if (host === "idp.localhost" && url.pathname === "/authorize") {
      return send(200, { "Content-Type": "text/html" }, SECURITY_KEY_PAGE);
    }
    return send(404, { "Content-Type": "text/plain" }, "not found");
  });
  await new Promise((resolve) => {
    server.listen(0, "127.0.0.1", resolve);
  });
  const port = server.address().port;
  return {
    port,
    workspaceUrl: `http://workspace.localhost:${port}/`,
    idpAuthorizePrefix: `http://idp.localhost:${port}/authorize`,
    requests,
    close: async () => {
      server.closeAllConnections();
      await new Promise((resolve) => {
        server.close(resolve);
      });
    },
  };
}

module.exports = { startSsoSites, WORKSPACE_SESSION };
