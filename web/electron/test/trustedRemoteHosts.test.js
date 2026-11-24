"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  trustedRemoteTarget,
  normalizeTrustedHostId,
  readTrustedRemoteHosts,
  isTrustedRemoteContext,
} = require("../src/trustedRemoteHosts");

const SERVER = "https://sandbox.example.com:8443/omnigent";
const HOST = "sandbox-host-1";
const entry = { serverUrl: trustedRemoteTarget(SERVER), hostId: HOST };
const context = { serverTarget: trustedRemoteTarget(SERVER), sourceHostId: HOST };

describe("trusted remote hosts", () => {
  it("canonicalizes server URLs the same way for entries and contexts", () => {
    assert.equal(trustedRemoteTarget(`${SERVER}/`), trustedRemoteTarget(SERVER));
    assert.equal(trustedRemoteTarget(`${SERVER}#section`), trustedRemoteTarget(SERVER));
    assert.equal(
      trustedRemoteTarget("https://sandbox.example.com:8443/omnigent?b=2&a=1"),
      trustedRemoteTarget("https://sandbox.example.com:8443/omnigent?a=1&b=2"),
    );
    assert.notEqual(trustedRemoteTarget("https://sandbox.example.com:8443/other"), entry.serverUrl);
    assert.equal(trustedRemoteTarget("ftp://sandbox.example.com/omnigent"), null);
    assert.equal(trustedRemoteTarget("https://user:pass@sandbox.example.com/omnigent"), null);
    assert.equal(trustedRemoteTarget("not a url"), null);
  });

  it("normalizes host IDs across case and the host_ prefix", () => {
    assert.equal(normalizeTrustedHostId("HOST_ABC"), "host_abc");
    assert.equal(normalizeTrustedHostId("host_ABC"), "abc");
    assert.equal(normalizeTrustedHostId(""), null);
    assert.equal(normalizeTrustedHostId(null), null);
  });

  it("keeps valid settings entries and drops malformed ones", () => {
    assert.deepEqual(readTrustedRemoteHosts([{ server_url: SERVER, host_id: HOST }]), [entry]);
    assert.deepEqual(
      readTrustedRemoteHosts([
        { server_url: SERVER, host_id: HOST },
        { server_url: SERVER, host_id: HOST },
        { server_url: "not a url", host_id: HOST },
        { server_url: SERVER },
        { server_url: SERVER, host_id: "" },
        "junk",
        null,
      ]),
      [entry],
    );
    assert.deepEqual(readTrustedRemoteHosts(undefined), []);
    assert.deepEqual(readTrustedRemoteHosts("https://sandbox.example.com"), []);
  });

  it("allows a session only on its trusted server and host", () => {
    assert.equal(isTrustedRemoteContext([entry], context, SERVER), true);
    assert.equal(
      isTrustedRemoteContext([entry], { ...context, sourceHostId: "host_1" }, SERVER),
      false,
    );
    assert.equal(
      isTrustedRemoteContext(
        [entry],
        { ...context, serverTarget: trustedRemoteTarget("https://other.example.com") },
        SERVER,
      ),
      false,
    );
    assert.equal(isTrustedRemoteContext([entry], context, "https://other.example.com"), false);
    assert.equal(isTrustedRemoteContext([entry], null, SERVER), false);
    assert.equal(isTrustedRemoteContext([entry], { serverTarget: entry.serverUrl }, SERVER), false);
    assert.equal(isTrustedRemoteContext([], context, SERVER), false);
  });
});
