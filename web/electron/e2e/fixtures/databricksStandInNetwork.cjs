// Preloaded before main.js: routes one stand-in Databricks host to a local fake
// workspace. Chromium reaches it through a CONNECT proxy (it cannot bind :443
// here) and trusts its self-signed certificate; main-process fetch is rewritten.
"use strict";

const { app, net, session } = require("electron");

const host = process.env.OMNIGENT_E2E_DATABRICKS_HOST;
const plainUrl = process.env.OMNIGENT_E2E_DATABRICKS_PLAIN_URL;
if (!host || !plainUrl) throw new Error("databricksStandInNetwork: host/plain URL not set");

app.commandLine.appendSwitch("proxy-server", plainUrl);

// The sandbox has only loopback, so Chromium reports offline; the stand-in host is local.
Object.defineProperty(net, "isOnline", { value: () => true, configurable: true });

app.whenReady().then(() => {
  session.defaultSession.setCertificateVerifyProc((request, callback) => {
    callback(request.hostname === host ? 0 : -3);
  });
});

const realFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  const raw = typeof input === "string" || input instanceof URL ? String(input) : input.url;
  const url = new URL(raw);
  if (url.hostname !== host && !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) {
    throw new Error("The desktop demo only allows its stand-in host and loopback");
  }
  if (url.protocol !== "https:" || url.hostname !== host) return realFetch(input, init);
  const rewritten = `${plainUrl}${url.pathname}${url.search}`;
  if (typeof input === "string" || input instanceof URL) return realFetch(rewritten, init);
  return realFetch(new Request(rewritten, input), init);
};
