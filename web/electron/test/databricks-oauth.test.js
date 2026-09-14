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
    calls.push({ url, body: init?.body });
    return responder(url, init, calls.length);
  });
  return calls;
}
const ok = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
const httpErr = (status, obj) => ({ ok: false, status, text: async () => JSON.stringify(obj) });

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

  it("throws when nothing is stored", async () => {
    await assert.rejects(oauth.getValidStoredToken(WS), /no stored Databricks token/);
  });
});
