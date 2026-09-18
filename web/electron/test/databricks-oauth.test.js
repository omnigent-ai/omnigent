// Unit tests for the Databricks token store + refresh lifecycle
// (src/databricks-oauth.js), run with `node --test` (no extra deps).
//
// Covers the parts the session-expiry and rotation logic depend on: the
// workspace-keyed token store round-trip, silent refresh endpoint selection
// (workspace-direct vs account/SPOG), single-use refresh-token rotation +
// persistence, concurrent-refresh dedupe, and dead-grant (invalid_grant)
// cleanup. Electron is stubbed so the module loads outside a packaged app.

"use strict";

const { describe, it, beforeEach, afterEach, mock } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Module = require("node:module");
const http = require("node:http");

// Stub `require("electron")` before loading the module under test. This file
// runs in its own node:test process, so the override doesn't leak elsewhere.
const electronStub = {
  shell: { openExternal: async () => {} },
  // Force the plaintext store path so the round-trip exercises real fs without
  // needing OS keychain encryption in CI.
  safeStorage: { isEncryptionAvailable: () => false },
  net: {},
};
// Bracket-access `_load` so no-underscore-dangle doesn't flag the Node API name.
const origLoad = Module["_load"];
Module["_load"] = function (request, ...rest) {
  if (request === "electron") return electronStub;
  return origLoad.call(this, request, ...rest);
};

const oauth = require("../src/databricks-oauth");

// Point the token store at a throwaway HOME per test.
let tmpHome;
beforeEach(() => {
  tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), "omni-oauth-"));
  mock.method(os, "homedir", () => tmpHome);
});
afterEach(() => {
  mock.restoreAll();
  fs.rmSync(tmpHome, { recursive: true, force: true });
});

const WS = "https://ws.cloud.databricks.com";
const ACCT = { origin: "https://accounts.cloud.databricks.com", id: "acc-123" };
const future = () => Math.floor(Date.now() / 1000) + 3600;
const past = () => Math.floor(Date.now() / 1000) - 10;

/** A fetch stub that records calls and returns a scripted token response. */
function mockTokenFetch(responder) {
  const calls = [];
  mock.method(globalThis, "fetch", async (url, init) => {
    calls.push({ url, body: init?.body, redirect: init?.redirect });
    return responder(url, init, calls.length);
  });
  return calls;
}
const ok = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
const httpErr = (status, obj) => ({ ok: false, status, text: async () => JSON.stringify(obj) });

function loopbackConfig(t) {
  const previous = process.env.OMNIGENT_DATABRICKS_OAUTH_REDIRECT;
  process.env.OMNIGENT_DATABRICKS_OAUTH_REDIRECT = "http://127.0.0.1";
  t.after(() => {
    if (previous === undefined)
      Reflect.deleteProperty(process.env, "OMNIGENT_DATABRICKS_OAUTH_REDIRECT");
    else process.env.OMNIGENT_DATABRICKS_OAUTH_REDIRECT = previous;
  });
}

/** Drive one real loopback callback and return its HTTP response plus the login result. */
async function runLoopbackCallback(t, buildCallback) {
  loopbackConfig(t);
  let opened;
  const browserOpened = new Promise((resolve) => {
    opened = resolve;
  });
  mock.method(electronStub.shell, "openExternal", async (url) => opened(new URL(url)));
  const calls = mockTokenFetch((_url, init) =>
    init?.method === "POST"
      ? ok({ access_token: "exchanged", expires_in: 3600 })
      : { status: 302, type: "basic" },
  );
  const login = oauth.runInteractiveLogin(WS);
  const settled = login.then(
    (value) => ({ value }),
    (error) => ({ error }),
  );
  const authorize = await browserOpened;
  const callback = new URL(authorize.searchParams.get("redirect_uri"));
  buildCallback(callback, authorize.searchParams.get("state"));
  const response = await new Promise((resolve, reject) => {
    http
      .get(callback, { agent: false }, (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          body += chunk;
        });
        res.on("end", () => resolve({ status: res.statusCode, body }));
      })
      .on("error", reject);
  });
  return { response, result: await settled, authorize, calls };
}

describe("loopback callback response", () => {
  it(
    "does not claim sign-in succeeded before the desktop finishes connecting",
    { timeout: 5000 },
    async (t) => {
      const { response, result, authorize, calls } = await runLoopbackCallback(
        t,
        (callback, state) => {
          callback.searchParams.set("state", state);
          callback.searchParams.set("code", "local-test-code");
        },
      );
      assert.equal(response.status, 200);
      assert.doesNotMatch(response.body, /Signed in/i);
      assert.match(response.body, /Return to Omnigent/i);
      assert.equal(result.value.tokens.access_token, "exchanged");
      // The browser flow and its preflight must agree on the one fixed redirect.
      assert.equal(new URL(calls[0].url).pathname, "/oidc/v1/authorize");
      const probed = new URL(calls[0].url).searchParams.get("redirect_uri");
      const used = new URL(authorize.searchParams.get("redirect_uri"));
      assert.equal(probed, "http://localhost");
      assert.equal(used.hostname, "localhost");
      assert.equal(used.pathname, "/");
      // Ephemeral port, so nothing is reserved and no other app can squat it.
      assert.notEqual(used.port, "");
    },
  );

  it("ignores a redirect override from the environment", { timeout: 5000 }, async (t) => {
    process.env.OMNIGENT_DATABRICKS_OAUTH_REDIRECT = "http://127.0.0.1:8765/callback";
    t.after(() => Reflect.deleteProperty(process.env, "OMNIGENT_DATABRICKS_OAUTH_REDIRECT"));
    const { result, authorize, calls } = await runLoopbackCallback(t, (callback, state) => {
      callback.searchParams.set("state", state);
      callback.searchParams.set("code", "local-test-code");
    });
    const used = new URL(authorize.searchParams.get("redirect_uri"));
    assert.equal(used.hostname, "localhost");
    assert.equal(used.pathname, "/");
    assert.notEqual(used.port, "8765");
    assert.equal(new URL(calls[0].url).searchParams.get("redirect_uri"), "http://localhost");
    assert.equal(result.value.tokens.access_token, "exchanged");
  });

  it("reports an authorization error instead of a success page", { timeout: 5000 }, async (t) => {
    const { response, result } = await runLoopbackCallback(t, (callback, state) => {
      callback.searchParams.set("state", state);
      callback.searchParams.set("error", "access_denied");
      callback.searchParams.set("error_description", "user refused");
    });
    assert.equal(response.status, 400);
    assert.doesNotMatch(response.body, /Signed in|received/i);
    assert.match(result.error.message, /access_denied/);
  });

  it("reports a callback with no code instead of a success page", { timeout: 5000 }, async (t) => {
    const { response, result } = await runLoopbackCallback(t, (callback, state) => {
      callback.searchParams.set("state", state);
    });
    assert.equal(response.status, 400);
    assert.doesNotMatch(response.body, /Signed in|received/i);
    assert.match(result.error.message, /no code in callback/);
  });
});

describe("interactive OAuth cancellation", () => {
  it("does not open a browser or issue requests when already cancelled", async () => {
    const controller = new AbortController();
    controller.abort();
    const fetchMock = mock.method(globalThis, "fetch", async () => {
      throw new Error("unexpected fetch");
    });
    const browser = mock.method(electronStub.shell, "openExternal", async () => {});
    await assert.rejects(
      oauth.runInteractiveLogin(WS, { signal: controller.signal }),
      (error) => error.name === "AbortError",
    );
    assert.equal(fetchMock.mock.callCount(), 0);
    assert.equal(browser.mock.callCount(), 0);
  });

  it("aborts the preflight without opening a browser", async (t) => {
    loopbackConfig(t);
    const controller = new AbortController();
    t.after(() => controller.abort());
    let requestSignal;
    mock.method(
      globalThis,
      "fetch",
      (_url, { signal }) =>
        new Promise((_resolve, reject) => {
          requestSignal = signal;
          signal.addEventListener("abort", () => reject(signal.reason), { once: true });
        }),
    );
    const browser = mock.method(electronStub.shell, "openExternal", async () => {});
    const login = oauth.runInteractiveLogin(WS, { signal: controller.signal });
    const rejected = assert.rejects(login, (error) => error.name === "AbortError");
    controller.abort();
    await rejected;
    assert.equal(requestSignal.aborted, true);
    assert.equal(browser.mock.callCount(), 0);
  });

  it(
    "closes the loopback listener when cancelled while waiting for the browser",
    { timeout: 5000 },
    async (t) => {
      loopbackConfig(t);
      const controller = new AbortController();
      t.after(() => controller.abort());
      mock.method(globalThis, "fetch", async () => ({ status: 302, type: "basic" }));
      let server;
      const createServer = http.createServer;
      mock.method(http, "createServer", (...args) => {
        server = createServer(...args);
        return server;
      });
      let opened;
      const browserOpened = new Promise((resolve) => {
        opened = resolve;
      });
      mock.method(electronStub.shell, "openExternal", async () => {
        opened();
      });
      const login = oauth.runInteractiveLogin(WS, { signal: controller.signal });
      const rejected = assert.rejects(login, (error) => error.name === "AbortError");
      await browserOpened;
      assert.equal(server.listening, true);
      let ignoredStatus;
      server.emit(
        "request",
        { url: "/?state=previous-login" },
        {
          writeHead: (status) => {
            ignoredStatus = status;
          },
          end() {},
        },
      );
      assert.equal(ignoredStatus, 400);
      assert.equal(server.listening, true);
      controller.abort();
      await rejected;
      assert.equal(server.listening, false);
      assert.equal(oauth.loadTokens(WS), null);
    },
  );

  it(
    "aborts a pending token exchange after a valid local callback",
    { timeout: 5000 },
    async (t) => {
      loopbackConfig(t);
      const controller = new AbortController();
      t.after(() => controller.abort());
      let opened;
      const browserOpened = new Promise((resolve) => {
        opened = resolve;
      });
      mock.method(electronStub.shell, "openExternal", async (url) => {
        opened(new URL(url));
      });
      let postStarted;
      const posting = new Promise((resolve) => {
        postStarted = resolve;
      });
      let postSignal;
      mock.method(globalThis, "fetch", async (_url, init) => {
        if (init.method !== "POST") return { status: 302, type: "basic" };
        postSignal = init.signal;
        postStarted();
        return new Promise((_resolve, reject) => {
          init.signal.addEventListener("abort", () => reject(init.signal.reason), { once: true });
        });
      });
      const login = oauth.runInteractiveLogin(WS, { signal: controller.signal });
      const rejected = assert.rejects(login, (error) => error.name === "AbortError");
      const authorize = await browserOpened;
      const callback = new URL(authorize.searchParams.get("redirect_uri"));
      callback.searchParams.set("state", authorize.searchParams.get("state"));
      callback.searchParams.set("code", "local-test-code");
      await new Promise((resolve, reject) => {
        http
          .get(callback, { agent: false }, (res) => {
            res.resume();
            res.on("end", resolve);
          })
          .on("error", reject);
      });
      await posting;
      controller.abort();
      await rejected;
      assert.equal(postSignal.aborted, true);
      assert.equal(oauth.loadTokens(WS), null);
    },
  );
});

describe("isTrustedDatabricksOrigin", () => {
  it("accepts https workspace and account hosts", () => {
    assert.equal(oauth.isTrustedDatabricksOrigin("https://ws.cloud.databricks.com"), true);
    assert.equal(oauth.isTrustedDatabricksOrigin("https://accounts.cloud.databricks.com"), true);
    assert.equal(oauth.isTrustedDatabricksOrigin("https://x.azuredatabricks.net"), true);
  });
  it("rejects look-alikes, http, and junk", () => {
    assert.equal(oauth.isTrustedDatabricksOrigin("https://evil-databricks.com"), false);
    assert.equal(oauth.isTrustedDatabricksOrigin("https://databricks.com.attacker.net"), false);
    assert.equal(oauth.isTrustedDatabricksOrigin("http://ws.cloud.databricks.com"), false);
    assert.equal(oauth.isTrustedDatabricksOrigin("not a url"), false);
  });
});

describe("token store round-trip (saveWorkspaceToken / loadTokens)", () => {
  it("persists keyed by workspace origin, with account context", () => {
    oauth.saveWorkspaceToken(
      WS,
      { access_token: "a", refresh_token: "r", expires_at: future() },
      ACCT,
    );
    const entry = oauth.loadTokens(WS);
    assert.equal(entry.access_token, "a");
    assert.equal(entry.refresh_token, "r");
    assert.deepEqual(entry.account, ACCT);
    // Not findable under the account origin — the expiry path looks up by workspace.
    assert.equal(oauth.loadTokens(ACCT.origin), null);
  });
  it("omits account context for a workspace-direct token", () => {
    oauth.saveWorkspaceToken(
      WS,
      { access_token: "a", refresh_token: "r", expires_at: future() },
      null,
    );
    assert.equal(oauth.loadTokens(WS).account, undefined);
  });
});

describe("access-token expiry simulation", () => {
  it("expires only the selected workspace's access token and preserves its refresh grant", async () => {
    const expiresAt = future();
    const sibling = "https://another.cloud.databricks.com";
    oauth.saveWorkspaceToken(
      WS,
      { access_token: "a", refresh_token: "r", expires_at: expiresAt },
      ACCT,
    );
    oauth.saveWorkspaceToken(
      sibling,
      { access_token: "other", refresh_token: "other-r", expires_at: expiresAt },
      null,
    );
    const calls = mockTokenFetch(() =>
      ok({ access_token: "new", refresh_token: "rotated", expires_in: 3600 }),
    );
    assert.equal(oauth.expireStoredAccessToken(WS), true);
    assert.equal(calls.length, 0);
    assert.deepEqual(oauth.loadTokens(WS), {
      access_token: "a",
      refresh_token: "r",
      expires_at: 0,
      account: ACCT,
    });
    assert.equal(oauth.loadTokens(sibling).expires_at, expiresAt);
    assert.equal(await oauth.getValidStoredToken(WS), "new");
    assert.equal(calls.length, 1);
    assert.equal(new URLSearchParams(calls[0].body).get("refresh_token"), "r");
  });
  it("does not invent a token when there is no stored login", () => {
    assert.equal(oauth.expireStoredAccessToken(WS), false);
    assert.equal(oauth.loadTokens(WS), null);
  });
});

describe("refresh-token invalidation", () => {
  it("removes only the cached refresh token without refreshing or expiring the access token", async () => {
    const expiresAt = future();
    const sibling = "https://another.cloud.databricks.com";
    oauth.saveWorkspaceToken(
      WS,
      { access_token: "a", refresh_token: "r", expires_at: expiresAt },
      ACCT,
    );
    oauth.saveWorkspaceToken(
      sibling,
      { access_token: "other", refresh_token: "other-r", expires_at: expiresAt },
      null,
    );
    const calls = mockTokenFetch(() => {
      throw new Error("No refresh request expected");
    });
    assert.equal(oauth.removeStoredRefreshToken(WS), true);
    assert.deepEqual(oauth.loadTokens(WS), {
      access_token: "a",
      expires_at: expiresAt,
      account: ACCT,
    });
    assert.equal(oauth.loadTokens(sibling).refresh_token, "other-r");
    assert.equal(await oauth.getValidStoredToken(WS), "a");
    assert.equal(calls.length, 0);
    oauth.expireStoredAccessToken(WS);
    await assert.rejects(oauth.getValidStoredToken(WS), {
      errorCode: "NO_REFRESH_TOKEN",
      message: /expired with no refresh token/,
    });
    assert.equal(calls.length, 0);
  });
  it("leaves an absent refresh token alone", () => {
    assert.equal(oauth.removeStoredRefreshToken(WS), false);
    assert.equal(oauth.loadTokens(WS), null);
    oauth.saveWorkspaceToken(WS, { access_token: "a", expires_at: future() }, null);
    assert.equal(oauth.removeStoredRefreshToken(WS), false);
    assert.equal(oauth.loadTokens(WS).access_token, "a");
  });
});

describe("token endpoint redirects", () => {
  for (const status of [302, 307, 308]) {
    it(`rejects HTTP ${status} without automatic redirect following or clearing the grant`, async () => {
      oauth.saveWorkspaceToken(
        WS,
        {
          access_token: "old",
          refresh_token: "r0",
          expires_at: past(),
        },
        status === 307 ? ACCT : null,
      );
      const before = oauth.loadTokens(WS);
      const calls = mockTokenFetch(() => ({
        ok: false,
        status,
        text: async () => "",
        headers: new Headers({
          location: `${WS}/oidc/v2/token`,
          "x-databricks-request-id": `redirect-${status}`,
        }),
      }));
      await assert.rejects(oauth.getValidStoredToken(WS), (error) => {
        assert.equal(error.status, status);
        assert.equal(error.requestId, `redirect-${status}`);
        assert.match(error.message, new RegExp(`token endpoint ${status}`));
        return true;
      });
      assert.equal(calls.length, 1);
      assert.equal(calls[0].redirect, "manual");
      assert.deepEqual(oauth.loadTokens(WS), before);
    });
  }
});

describe("getValidStoredToken", () => {
  it("returns a still-valid token without a network call", async () => {
    oauth.saveWorkspaceToken(
      WS,
      { access_token: "live", refresh_token: "r", expires_at: future() },
      null,
    );
    const calls = mockTokenFetch(() =>
      ok({ access_token: "SHOULD_NOT_BE_USED", expires_in: 3600 }),
    );
    assert.equal(await oauth.getValidStoredToken(WS), "live");
    assert.equal(calls.length, 0);
  });

  it("workspace-direct: refreshes at the workspace token endpoint", async () => {
    oauth.saveWorkspaceToken(
      WS,
      { access_token: "old", refresh_token: "r0", expires_at: past() },
      null,
    );
    const calls = mockTokenFetch(() =>
      ok({ access_token: "new", refresh_token: "r1", expires_in: 3600 }),
    );
    assert.equal(await oauth.getValidStoredToken(WS), "new");
    assert.equal(calls[0].url, `${WS}/oidc/v1/token`);
  });

  it("SPOG: refreshes at the account token endpoint with the account id in the path", async () => {
    oauth.saveWorkspaceToken(
      WS,
      { access_token: "old", refresh_token: "r0", expires_at: past() },
      ACCT,
    );
    const calls = mockTokenFetch(() =>
      ok({ access_token: "new", refresh_token: "r1", expires_in: 3600 }),
    );
    await oauth.getValidStoredToken(WS);
    assert.equal(calls[0].url, `${ACCT.origin}/oidc/accounts/${ACCT.id}/v1/token`);
  });

  it("persists the rotated (single-use) refresh token", async () => {
    oauth.saveWorkspaceToken(
      WS,
      { access_token: "old", refresh_token: "r0", expires_at: past() },
      null,
    );
    mockTokenFetch(() => ok({ access_token: "new", refresh_token: "r1", expires_in: 3600 }));
    await oauth.getValidStoredToken(WS);
    assert.equal(oauth.loadTokens(WS).refresh_token, "r1");
  });

  it("keeps the old refresh token when the server does not rotate", async () => {
    oauth.saveWorkspaceToken(
      WS,
      { access_token: "old", refresh_token: "r0", expires_at: past() },
      null,
    );
    mockTokenFetch(() => ok({ access_token: "new", expires_in: 3600 })); // no refresh_token
    await oauth.getValidStoredToken(WS);
    assert.equal(oauth.loadTokens(WS).refresh_token, "r0");
  });

  it("dedupes concurrent refreshes into a single request (single-use safety)", async () => {
    oauth.saveWorkspaceToken(
      WS,
      { access_token: "old", refresh_token: "r0", expires_at: past() },
      null,
    );
    const calls = mockTokenFetch(async () => {
      await new Promise((r) => {
        setTimeout(r, 20);
      });
      return ok({ access_token: "new", refresh_token: "r1", expires_in: 3600 });
    });
    const [a, b] = await Promise.all([
      oauth.getValidStoredToken(WS),
      oauth.getValidStoredToken(WS),
    ]);
    assert.equal(a, "new");
    assert.equal(b, "new");
    assert.equal(calls.length, 1); // one token in flight, not two
  });

  it("clears the stored token on a dead grant (invalid_grant) so we re-login", async () => {
    oauth.saveWorkspaceToken(
      WS,
      { access_token: "old", refresh_token: "r0", expires_at: past() },
      null,
    );
    mockTokenFetch(() => httpErr(400, { error: "invalid_grant" }));
    await assert.rejects(oauth.getValidStoredToken(WS));
    assert.equal(oauth.loadTokens(WS), null);
  });

  it("logs token request metadata without credentials or raw error responses", async () => {
    const logs = [];
    mock.method(console, "log", (...args) => logs.push(args));
    mock.method(console, "warn", (...args) => logs.push(args));
    oauth.saveWorkspaceToken(
      WS,
      {
        access_token: "SENSITIVE_TEST_ACCESS_VALUE",
        refresh_token: "SENSITIVE_TEST_REFRESH_VALUE",
        expires_at: past(),
      },
      null,
    );
    mockTokenFetch(() => ({
      ...httpErr(403, {
        error: "invalid_scope",
        error_description: "SENSITIVE_TEST_RESPONSE_VALUE",
      }),
      headers: new Headers({ "x-databricks-request-id": "trace-test" }),
    }));
    await assert.rejects(oauth.getValidStoredToken(WS), (error) => {
      assert.equal(error.status, 403);
      assert.equal(error.errorCode, "invalid_scope");
      assert.equal(error.requestId, "trace-test");
      assert.doesNotMatch(error.message, /SENSITIVE_TEST/);
      return true;
    });
    const output = JSON.stringify(logs);
    assert.match(output, /token request/);
    assert.match(output, /token response/);
    assert.match(output, /refresh_token/);
    assert.match(output, /invalid_scope/);
    assert.doesNotMatch(output, /SENSITIVE_TEST/);
  });

  it("throws when nothing is stored", async () => {
    await assert.rejects(oauth.getValidStoredToken(WS), {
      errorCode: "NO_STORED_TOKEN",
      message: /no stored Databricks token/,
    });
  });
});
