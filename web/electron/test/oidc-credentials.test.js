// Unit tests for OIDC sign-in credentials (src/oidc-credentials.js), run with
// `node --test`. The loopback listener is real; the system browser is a
// callback that follows the server's redirect to it, and the server's token
// endpoints are a fetch stub. Electron is stubbed so the module loads outside
// the app.

"use strict";

const { describe, it, beforeEach, afterEach, mock } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const Module = require("node:module");

const electronStub = {
  shell: { openExternal: async () => {} },
  // Plaintext store so the round trip runs without an OS keychain.
  safeStorage: { isEncryptionAvailable: () => false },
};
const origLoad = Module["_load"];
Module["_load"] = function (request, ...rest) {
  if (request === "electron") return electronStub;
  return origLoad.call(this, request, ...rest);
};

const oidc = require("../src/oidc-credentials");

const SERVER = "https://omni.example";
let tmpHome;
beforeEach(() => {
  tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), "omni-oidc-"));
  mock.method(os, "homedir", () => tmpHome);
  mock.method(console, "log", () => {});
  mock.method(console, "warn", () => {});
});
afterEach(() => {
  mock.restoreAll();
  fs.rmSync(tmpHome, { recursive: true, force: true });
});

const storeFile = () => path.join(tmpHome, ".omnigent", "oidc_tokens.json");
const readStore = () => JSON.parse(fs.readFileSync(storeFile(), "utf8"));
const writeStore = (value) => {
  fs.mkdirSync(path.dirname(storeFile()), { recursive: true });
  fs.writeFileSync(storeFile(), JSON.stringify(value));
};

function json(status, body) {
  return { status, json: async () => body };
}

/** Record fetch calls and answer them with `responder`. */
function stubFetch(responder) {
  const calls = [];
  mock.method(globalThis, "fetch", async (url, init) => {
    calls.push({ url: String(url), init, form: new URLSearchParams(init?.body ?? "") });
    return responder(String(url), init, calls.length);
  });
  return calls;
}

function get(url) {
  return new Promise((resolve, reject) => {
    http
      .get(url, { agent: false }, (res) => {
        res.resume();
        res.on("end", () => resolve(res.statusCode));
      })
      .on("error", reject);
  });
}

/** A system browser that completes sign-in by hitting the loopback with `params`. */
function browserReturning(params) {
  const opened = [];
  const openExternal = async (raw) => {
    const url = new URL(raw);
    opened.push(url);
    const callback = new URL(url.searchParams.get("native_redirect_uri"));
    callback.searchParams.set("state", url.searchParams.get("native_state"));
    for (const [key, value] of Object.entries(params)) callback.searchParams.set(key, value);
    setImmediate(() => void get(callback));
  };
  return { opened, openExternal };
}

describe("serverEndpoint", () => {
  it("joins routes under the server mount and drops query and hash", () => {
    assert.equal(
      oidc.serverEndpoint("https://h.test/", "/auth/login"),
      "https://h.test/auth/login",
    );
    assert.equal(
      oidc.serverEndpoint("https://h.test/omnigent/?x=1#y", "/v1/me"),
      "https://h.test/omnigent/v1/me",
    );
  });
});

describe("signInWithBrowser", () => {
  it("runs the native loopback flow and stores the refresh grant", { timeout: 5000 }, async () => {
    const calls = stubFetch(() =>
      json(200, {
        token: "session-jwt",
        user_id: "alice@example.com",
        expires_in: 28800,
        refresh_token: "refresh-1",
      }),
    );
    const browser = browserReturning({ code: "one-time" });
    const minted = await oidc.signInWithBrowser(SERVER, { openExternal: browser.openExternal });

    assert.deepEqual(minted, { token: "session-jwt", expiresIn: 28800 });
    const [login] = browser.opened;
    assert.equal(`${login.origin}${login.pathname}`, `${SERVER}/auth/login`);
    assert.equal(login.searchParams.get("code_challenge_method"), "S256");
    const redirect = new URL(login.searchParams.get("native_redirect_uri"));
    assert.equal(redirect.hostname, "127.0.0.1");
    assert.equal(redirect.pathname, "/callback");

    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, `${SERVER}/auth/native-token`);
    assert.equal(calls[0].init.redirect, "manual");
    assert.equal(calls[0].form.get("code"), "one-time");
    assert.equal(calls[0].form.get("redirect_uri"), redirect.toString());
    // The verifier hashes to the challenge the browser carried.
    const crypto = require("node:crypto");
    const digest = crypto
      .createHash("sha256")
      .update(calls[0].form.get("code_verifier"))
      .digest("base64url");
    assert.equal(digest, login.searchParams.get("code_challenge"));

    assert.deepEqual(readStore()[SERVER].plain, {
      refresh_token: "refresh-1",
      user_id: "alice@example.com",
    });
    assert.equal(oidc.hasStoredGrant(SERVER), true);
  });

  it("drops an older grant when the server issues none", async () => {
    writeStore({ [SERVER]: { plain: { refresh_token: "old" } } });
    stubFetch(() => json(200, { token: "t", user_id: "u", expires_in: 60 }));
    const browser = browserReturning({ code: "c" });
    await oidc.signInWithBrowser(SERVER, { openExternal: browser.openExternal });
    assert.equal(oidc.hasStoredGrant(SERVER), false);
  });

  it("reports the server's reason when sign-in is refused", async () => {
    const calls = stubFetch(() => json(500, {}));
    const browser = browserReturning({
      error: "access_denied",
      error_description: "Email domain 'x.test' is not permitted on this server",
    });
    await assert.rejects(
      oidc.signInWithBrowser(SERVER, { openExternal: browser.openExternal }),
      (error) =>
        error.code === "sign_in_failed" &&
        error.description === "Email domain 'x.test' is not permitted on this server",
    );
    assert.equal(calls.length, 0, "no exchange without a code");
  });

  it("fails when the exchange is rejected", async () => {
    stubFetch(() => json(400, { error: "invalid_grant" }));
    const browser = browserReturning({ code: "c" });
    await assert.rejects(
      oidc.signInWithBrowser(SERVER, { openExternal: browser.openExternal }),
      (error) => error.code === "sign_in_failed",
    );
  });

  it("maps an unopenable browser", async () => {
    await assert.rejects(
      oidc.signInWithBrowser(SERVER, { openExternal: async () => Promise.reject(new Error("x")) }),
      (error) => error.code === "browser_unavailable",
    );
  });

  it("stops when cancelled", async () => {
    const controller = new AbortController();
    const openExternal = async () => controller.abort();
    await assert.rejects(
      oidc.signInWithBrowser(SERVER, { openExternal, signal: controller.signal }),
      (error) => error.name === "AbortError",
    );
  });
});

describe("refreshSession", () => {
  it("mints a session from the stored grant", async () => {
    writeStore({ [SERVER]: { plain: { refresh_token: "refresh-1" } } });
    const calls = stubFetch(() => json(200, { access_token: "fresh", expires_in: 3600 }));
    assert.deepEqual(await oidc.refreshSession(`${SERVER}/`), { token: "fresh", expiresIn: 3600 });
    assert.equal(calls[0].url, `${SERVER}/oauth/token`);
    assert.equal(calls[0].form.get("grant_type"), "refresh_token");
    assert.equal(calls[0].form.get("refresh_token"), "refresh-1");
  });

  it("shares one request between concurrent callers", async () => {
    writeStore({ [SERVER]: { plain: { refresh_token: "r" } } });
    const calls = stubFetch(() => json(200, { access_token: "fresh", expires_in: 3600 }));
    await Promise.all([oidc.refreshSession(SERVER), oidc.refreshSession(SERVER)]);
    assert.equal(calls.length, 1);
  });

  it("requires a stored grant", async () => {
    const calls = stubFetch(() => json(200, {}));
    await assert.rejects(oidc.refreshSession(SERVER), (e) => e.code === "NO_STORED_TOKEN");
    assert.equal(calls.length, 0);
  });

  for (const [status, error] of [
    [400, "invalid_grant"],
    [400, "expired_token"],
  ]) {
    it(`forgets a dead grant (${error})`, async () => {
      writeStore({ [SERVER]: { plain: { refresh_token: "r" } } });
      stubFetch(() => json(status, { error }));
      await assert.rejects(oidc.refreshSession(SERVER), (e) => e.code === error);
      assert.equal(oidc.hasStoredGrant(SERVER), false);
    });
  }

  it("forgets the grant when the server has no token route", async () => {
    writeStore({ [SERVER]: { plain: { refresh_token: "r" } } });
    stubFetch(() => json(404, null));
    await assert.rejects(oidc.refreshSession(SERVER), (e) => e.code === "NO_STORED_TOKEN");
    assert.equal(oidc.hasStoredGrant(SERVER), false);
  });

  it("keeps the grant when the server is unreachable", async () => {
    writeStore({ [SERVER]: { plain: { refresh_token: "r" } } });
    mock.method(globalThis, "fetch", async () => {
      throw new Error("ECONNREFUSED");
    });
    await assert.rejects(oidc.refreshSession(SERVER), (e) => e.code === "network");
    assert.equal(oidc.hasStoredGrant(SERVER), true);
  });
});

describe("hasStoredGrant", () => {
  it("does not count an empty refresh token as a grant", () => {
    writeStore({ [SERVER]: { plain: { refresh_token: "" } } });
    assert.equal(oidc.hasStoredGrant(SERVER), false);
  });
});

describe("signOut", () => {
  it("revokes and forgets the stored grant", async () => {
    writeStore({ [SERVER]: { plain: { refresh_token: "r" } } });
    const calls = stubFetch(() => json(200, { revoked: true }));
    await oidc.signOut(SERVER);
    assert.equal(calls[0].url, `${SERVER}/oauth/revoke`);
    assert.equal(calls[0].form.get("refresh_token"), "r");
    assert.equal(oidc.hasStoredGrant(SERVER), false);
  });

  it("forgets the grant even when revocation fails", async () => {
    writeStore({ [SERVER]: { plain: { refresh_token: "r" } } });
    mock.method(globalThis, "fetch", async () => {
      throw new Error("offline");
    });
    await oidc.signOut(SERVER);
    assert.equal(oidc.hasStoredGrant(SERVER), false);
  });

  it("forgets the grant and warns when the server refuses to revoke", async () => {
    writeStore({ [SERVER]: { plain: { refresh_token: "r" } } });
    stubFetch(() => json(503, null));
    await oidc.signOut(SERVER);
    assert.equal(oidc.hasStoredGrant(SERVER), false);
    assert.ok(console.warn.mock.calls.some((call) => /HTTP 503/.test(call.arguments[0])));
  });

  it("forgets the grant before the revoke request returns", async () => {
    writeStore({ [SERVER]: { plain: { refresh_token: "r" } } });
    let respond;
    mock.method(
      globalThis,
      "fetch",
      () =>
        new Promise((resolve) => {
          respond = () => resolve(json(200, {}));
        }),
    );
    const pending = oidc.signOut(SERVER);
    assert.equal(oidc.hasStoredGrant(SERVER), false);
    respond();
    await pending;
  });

  it("makes no request without a stored grant", async () => {
    const calls = stubFetch(() => json(200, {}));
    await oidc.signOut(SERVER);
    assert.equal(calls.length, 0);
  });
});
