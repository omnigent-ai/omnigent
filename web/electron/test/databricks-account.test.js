// Unit tests for account-first (SPOG) workspace resolution
// (src/databricks-account.js), run with `node --test`.
//
// Covers reading the account origin+id from an account-scoped token's issuer,
// distinguishing it from a workspace-scoped token, the RUNNING-workspace
// filtering of the account workspaces API, and the refusal to send the bearer
// to an untrusted account origin.

"use strict";

const { describe, it, afterEach, mock } = require("node:test");
const assert = require("node:assert/strict");
const Module = require("node:module");

// databricks-account requires databricks-oauth, which requires electron.
const electronStub = { shell: {}, safeStorage: { isEncryptionAvailable: () => false }, net: {} };
// Bracket-access `_load` so no-underscore-dangle doesn't flag the Node API name.
const origLoad = Module["_load"];
Module["_load"] = function (request, ...rest) {
  if (request === "electron") return electronStub;
  return origLoad.call(this, request, ...rest);
};

const { parseAccountFromToken, listRunningWorkspaces } = require("../src/databricks-account");

/** Build a JWT-shaped token string with the given `iss` claim (only the payload matters here). */
function tokenWithIss(iss) {
  const payload = Buffer.from(JSON.stringify({ iss })).toString("base64url");
  return `header.${payload}.sig`;
}

afterEach(() => mock.restoreAll());

describe("parseAccountFromToken", () => {
  it("extracts account origin + id from an account-scoped issuer", () => {
    const t = tokenWithIss("https://accounts.cloud.databricks.com/oidc/accounts/acc-123");
    assert.deepEqual(parseAccountFromToken(t), {
      accountOrigin: "https://accounts.cloud.databricks.com",
      accountId: "acc-123",
    });
  });

  it("returns null for a workspace-scoped issuer (no account path)", () => {
    assert.equal(parseAccountFromToken(tokenWithIss("https://ws.cloud.databricks.com/oidc")), null);
  });

  it("returns null for a malformed or issuer-less token", () => {
    assert.equal(parseAccountFromToken("not-a-jwt"), null);
    assert.equal(parseAccountFromToken(tokenWithIss("")), null);
  });
});

describe("listRunningWorkspaces", () => {
  const account = { accountOrigin: "https://accounts.cloud.databricks.com", accountId: "acc-123" };

  it("returns only RUNNING workspaces with an fqdn, mapped to {id,name,fqdn}", async () => {
    const rows = [
      {
        workspace_id: 1,
        workspace_name: "alpha",
        workspace_status: "RUNNING",
        workspace_fqdn: "a.cloud.databricks.com",
      },
      {
        workspace_id: 2,
        workspace_name: "beta",
        workspace_status: "CANCELLED",
        workspace_fqdn: "b.cloud.databricks.com",
      },
      { workspace_id: 3, workspace_name: "gamma", workspace_status: "RUNNING" }, // no fqdn → dropped
    ];
    let calledUrl;
    mock.method(globalThis, "fetch", async (url) => {
      calledUrl = url;
      return { ok: true, status: 200, json: async () => rows };
    });
    const result = await listRunningWorkspaces(account, "tok");
    assert.equal(
      calledUrl,
      "https://accounts.cloud.databricks.com/api/2.0/accounts/acc-123/workspaces",
    );
    assert.deepEqual(result, [{ workspaceId: "1", name: "alpha", fqdn: "a.cloud.databricks.com" }]);
  });

  it("refuses to send the bearer to an untrusted account origin", async () => {
    const fetchMock = mock.method(globalThis, "fetch", async () => ({
      ok: true,
      json: async () => [],
    }));
    await assert.rejects(
      listRunningWorkspaces({ accountOrigin: "https://evil.com", accountId: "x" }, "tok"),
      /untrusted account origin/,
    );
    assert.equal(fetchMock.mock.callCount(), 0); // never hit the network
  });

  it("rejects a redirect instead of carrying the bearer to its target", async () => {
    const calls = [];
    mock.method(globalThis, "fetch", async (url, init) => {
      calls.push({ url, redirect: init?.redirect });
      return { ok: false, status: 302, text: async () => "" };
    });
    await assert.rejects(listRunningWorkspaces(account, "tok"), /workspaces lookup failed: 302/);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].redirect, "manual");
  });

  it("cancels the workspace lookup without continuing the login", async () => {
    const controller = new AbortController();
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
    const lookup = listRunningWorkspaces(account, "tok", { signal: controller.signal });
    const rejected = assert.rejects(lookup, (error) => error.name === "AbortError");
    controller.abort();
    await rejected;
    assert.equal(requestSignal.aborted, true);
  });

  it("throws on a non-ok response", async () => {
    mock.method(globalThis, "fetch", async () => ({
      ok: false,
      status: 403,
      text: async () => "denied",
    }));
    await assert.rejects(listRunningWorkspaces(account, "tok"), /workspaces lookup failed: 403/);
  });
});
