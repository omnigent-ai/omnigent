// Unit tests for the shared RFC 8252 loopback runner (src/loopback-oauth.js),
// run with `node --test`. A real listener answers real HTTP requests; only the
// system browser is replaced by a callback that reports the authorize URL.

"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const http = require("node:http");

const { makePkce, runLoopbackAuthorization } = require("../src/loopback-oauth");

const pages = {
  received: ["Received", "Return to the app."],
  failed: ["Failed", "Try again."],
  incomplete: ["Incomplete", "Try again."],
};

/** Start a run; resolve once the "browser" was opened with the authorize URL. */
function start(options = {}) {
  let opened;
  const browser = new Promise((resolve) => {
    opened = resolve;
  });
  const run = runLoopbackAuthorization({
    hostname: "127.0.0.1",
    callbackPath: "/callback",
    redirectUri: (port) => `http://127.0.0.1:${port}/callback`,
    authorizeUrl: (redirectUri, state) =>
      `https://idp.test/authorize?${new URLSearchParams({ redirect_uri: redirectUri, state })}`,
    openExternal: async (url) => opened(new URL(url)),
    pages,
    ...options,
  });
  const settled = run.then(
    (value) => ({ value }),
    (error) => ({ error }),
  );
  return { browser, settled };
}

function get(url) {
  return new Promise((resolve, reject) => {
    http
      .get(url, { agent: false }, (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          body += chunk;
        });
        res.on("end", () => resolve({ status: res.statusCode, body }));
      })
      .on("error", reject);
  });
}

function callbackUrl(authorize, params) {
  const url = new URL(authorize.searchParams.get("redirect_uri"));
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
  return url;
}

describe("runLoopbackAuthorization", () => {
  it("resolves with the callback params and the redirect URI", { timeout: 5000 }, async () => {
    const { browser, settled } = start();
    const authorize = await browser;
    const state = authorize.searchParams.get("state");
    const response = await get(callbackUrl(authorize, { state, code: "abc" }));
    const { value } = await settled;
    assert.equal(response.status, 200);
    assert.match(response.body, /Received/);
    assert.equal(value.params.get("code"), "abc");
    assert.equal(value.redirectUri, authorize.searchParams.get("redirect_uri"));
  });

  it("ignores other paths and stale state, then still accepts the real callback", async () => {
    const { browser, settled } = start();
    const authorize = await browser;
    const state = authorize.searchParams.get("state");
    const other = new URL(authorize.searchParams.get("redirect_uri"));
    other.pathname = "/favicon.ico";
    assert.equal((await get(other)).status, 404);
    assert.equal((await get(callbackUrl(authorize, { state: "old", code: "x" }))).status, 400);
    await get(callbackUrl(authorize, { state, code: "real" }));
    assert.equal((await settled).value.params.get("code"), "real");
  });

  it("rejects an error callback with its code and description", async () => {
    const { browser, settled } = start();
    const authorize = await browser;
    const state = authorize.searchParams.get("state");
    const response = await get(
      callbackUrl(authorize, {
        state,
        error: "access_denied",
        error_description: "Email domain 'x.test' is not permitted",
      }),
    );
    const { error } = await settled;
    assert.equal(response.status, 400);
    assert.match(response.body, /Failed/);
    assert.equal(error.errorCode, "access_denied");
    assert.equal(error.errorDescription, "Email domain 'x.test' is not permitted");
  });

  it("rejects a callback without a code", async () => {
    const { browser, settled } = start();
    const authorize = await browser;
    const state = authorize.searchParams.get("state");
    const response = await get(callbackUrl(authorize, { state }));
    assert.match(response.body, /Incomplete/);
    assert.match((await settled).error.message, /no code in callback/);
  });

  it("stops listening when cancelled", async () => {
    const controller = new AbortController();
    const { browser, settled } = start({ signal: controller.signal });
    const authorize = await browser;
    controller.abort(Object.assign(new Error("cancelled"), { name: "AbortError" }));
    assert.equal((await settled).error.name, "AbortError");
    await assert.rejects(get(callbackUrl(authorize, { state: "s", code: "c" })));
  });

  it("fails fast when the browser cannot be opened", async () => {
    const { settled } = start({
      openExternal: async () => Promise.reject(new Error("no browser")),
    });
    const { error } = await settled;
    assert.match(error.message, /could not open the system browser: no browser/);
    assert.equal(error.code, "BROWSER_UNAVAILABLE");
  });

  it("times out", async () => {
    const { settled } = start({ timeoutMs: 20, openExternal: async () => {} });
    const { error } = await settled;
    assert.match(error.message, /timed out/);
    assert.equal(error.code, "LOOPBACK_TIMEOUT");
  });
});

describe("makePkce", () => {
  it("derives an S256 challenge from a 43+ character verifier", () => {
    const { verifier, challenge } = makePkce();
    assert.match(verifier, /^[A-Za-z0-9_-]{43,128}$/);
    const expected = crypto
      .createHash("sha256")
      .update(verifier)
      .digest("base64")
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
    assert.equal(challenge, expected);
  });
});
