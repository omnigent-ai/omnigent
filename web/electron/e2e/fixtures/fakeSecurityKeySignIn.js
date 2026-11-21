// A stand-in Databricks App sign-in for desktop e2e runs: Chromium maps the app
// host to a local HTTPS front door that redirects to an Okta-style factor page.

"use strict";

const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const path = require("node:path");
const { execFileSync, spawnSync } = require("node:child_process");

const opensslAvailable = () => spawnSync("openssl", ["version"]).status === 0;

// An Okta-style factor page that requests an assertion as soon as it loads.
const securityKeyVerifyPage = ({ timeoutMs }) => `<!doctype html><title>Sign In</title>
<style>body { background: #fff; color: #1d1d21; font: 18px sans-serif; padding: 48px; }</style>
<h2>Verify with Security Key or Biometric Authenticator</h2>
<p id="status">You will be prompted to use a security key or biometric verification.</p>
<script>
window.verifyState = { outcome: "idle" };
setTimeout(async () => {
  window.verifyState.outcome = "pending";
  try {
    await navigator.credentials.get({ publicKey: {
      challenge: crypto.getRandomValues(new Uint8Array(32)),
      rpId: "localhost",
      allowCredentials: [{ type: "public-key", id: crypto.getRandomValues(new Uint8Array(16)) }],
      userVerification: "preferred",
      timeout: ${timeoutMs},
    } });
    window.verifyState.outcome = "signed-in";
  } catch (error) {
    window.verifyState.outcome = error.name;
  }
  document.getElementById("status").textContent = "Result: " + window.verifyState.outcome;
}, 500);
</script>`;

const listen = (server) =>
  new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve(server.address().port));
  });

/**
 * @param {{ scratch: string, appHost: string }} options
 * @returns {Promise<{ appUrl: string, verifyUrl: string, preload: string,
 *   frontDoorRequests: string[], verifyPageLoads: string[], close: () => Promise<void> }>}
 */
async function startFakeSecurityKeySignIn({ scratch, appHost }) {
  const verifyPageLoads = [];
  const idp = http.createServer((request, response) => {
    verifyPageLoads.push(request.url);
    response.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-store" });
    response.end(securityKeyVerifyPage({ timeoutMs: 120_000 }));
  });
  const frontDoorRequests = [];
  let frontDoor;
  const close = async () => {
    for (const server of [idp, frontDoor]) {
      if (!server?.listening) continue;
      server.closeAllConnections();
      // oxlint-disable-next-line no-await-in-loop -- close each server in turn.
      await new Promise((resolve) => {
        server.close(resolve);
      });
    }
  };

  try {
    // WebAuthn needs a secure context; loopback `localhost` is one.
    const verifyUrl = `http://localhost:${await listen(idp)}/okta/verify`;

    const key = path.join(scratch, "front-door-key.pem");
    const cert = path.join(scratch, "front-door-cert.pem");
    const subject = ["-subj", `/CN=${appHost}`, "-addext", `subjectAltName=DNS:${appHost}`];
    execFileSync(
      "openssl",
      [
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        ...subject,
        "-keyout",
        key,
        "-out",
        cert,
      ],
      { stdio: "pipe" },
    );
    frontDoor = https.createServer(
      { key: fs.readFileSync(key), cert: fs.readFileSync(cert) },
      (request, response) => {
        frontDoorRequests.push(request.url);
        response.writeHead(302, { Location: verifyUrl, "Cache-Control": "no-store" });
        response.end();
      },
    );
    const frontDoorPort = await listen(frontDoor);

    const preload = path.join(scratch, "front-door-network.cjs");
    // The proxy switch keeps a CI egress proxy from intercepting the mapped host.
    fs.writeFileSync(
      preload,
      `const { app } = require("electron");\n` +
        `app.commandLine.appendSwitch("host-resolver-rules", ${JSON.stringify(
          `MAP ${appHost}:443 127.0.0.1:${frontDoorPort}`,
        )});\n` +
        `app.commandLine.appendSwitch("ignore-certificate-errors");\n` +
        `app.commandLine.appendSwitch("no-proxy-server");\n`,
    );

    return {
      appUrl: `https://${appHost}`,
      verifyUrl,
      preload,
      frontDoorRequests,
      verifyPageLoads,
      close,
    };
  } catch (error) {
    await close();
    throw error;
  }
}

module.exports = { opensslAvailable, securityKeyVerifyPage, startFakeSecurityKeySignIn };
